"""
pretrain.py

Pretraining script for Prismatic VLM pretraining in native PyTorch, using Fully-Sharded Data Parallel (FSDP) to run
distributed training across GPUs. By default, assumes that CUDA toolkit is >= 11.0 (to support BF16 mixed precision).


Notes & Prerequisites:
    - We're loading Llama-2 (and possibly other) gated models from HuggingFace (HF Hub); these require an auth_token.
      For Llama-2, make sure to first get Meta approval, then fill out the form at the top of the HF Llama-2 page:
        => Link: https://huggingface.co/meta-llama/Llama-2-7b-chat-hf
        => Generate Token (from `huggingface.co`): Settings / Access Tokens / New "Read" Token
        => Set `cfg.hf_token` to file path with token (as single line text file) or environment variable name

    - If you want to set a custom location for all HF / TIMM artifacts --> `export HF_HOME="<PATH>"` *before* running!
        => For example (add to end of .bashrc): `export HF_HOME="/mnt/fsx/skaramcheti/cache"`

Run with:
    - [Single Node One-GPU (Debug)] : torchrun --standalone --nnodes 1 --nproc-per-node 1 scripts/pretrain.py
    - [Single Node Multi-GPU (= $K)]: torchrun --standalone --nnodes 1 --nproc-per-node $K scripts/pretrain.py
    - [Multi-Node/AWS Sagemaker] Depends on your individual setup; file an issue if you have trouble!
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, Union

import draccus
import torch
import torch.distributed as dist
import yaml

from prismatic.conf import DatasetConfig, DatasetRegistry, ModelConfig, ModelRegistry
from prismatic.models import get_llm_backbone_and_tokenizer, get_vision_backbone_and_transform, get_vlm
from prismatic.overwatch import initialize_overwatch
from prismatic.preprocessing import get_dataset_and_collator
from prismatic.training import Metrics, get_train_strategy
from prismatic.util import set_global_seed

from huggingface_hub import hf_hub_download
from PIL import Image

# Disable Tokenizers Parallelism to Play Nice w/ PyTorch Multiprocessing DataLoaders
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Initialize Overwatch =>> Wraps `logging.Logger`
overwatch = initialize_overwatch(__name__)


@dataclass
class PretrainConfig:
    # fmt: off

    # ModelConfig (`prismatic/conf/models.py`); override with --model.type `ModelRegistry.<MODEL>.model_id`
    model: ModelConfig = field(
        default_factory=ModelConfig.get_choice_class(ModelRegistry.PRISM_CLIP_7B.model_id)  # PRISM_DINOSIGLIP_7B
    )

    # DatasetConfig (`prismatic/conf/datasets.py`); override with --dataset.type `DatasetRegistry.<DATASET>.dataset_id`
    dataset: DatasetConfig = field(
        default_factory=DatasetConfig.get_choice_class(DatasetRegistry.WEBVID.dataset_id)  # TODO LLAVA_V15
    )

    val_dataset: DatasetConfig = field(
        default_factory=DatasetConfig.get_choice_class(DatasetRegistry.WEBVID.dataset_id)  # TODO WEBVID_VAL
    )
    
    # Pretraining Stage in < align (projector-only) | finetune (projector + LLM) | full-finetune (all) >
    # ---
    stage: str = "finetune"                                         # Pretraining Stage in < align | finetune >
    pretrained_checkpoint: Optional[Path] = None                    # Pretrained Checkpoint to Load (for `finetune`)
                                                                    #   if None =>> will match on (run_dir / `align`)
    
    # Run Arguments
    run_id: Optional[str] = None                                    # Run ID for logging, Weights & Biases
    run_root_dir: Path = Path("runs")                               # Path to directory to store logs & checkpoints
    seed: int = 7                                                   # Random seed (for reproducibility)

    # HF Hub Credentials (for any gated models)
    hf_token: Union[str, Path] = Path(".hf_token")                  # Environment variable or Path to HF Token

    # Tracking Parameters
    trackers: Tuple[str, ...] = ("jsonl", "wandb")                  # Trackers to initialize (if W&B, add config!)
    wandb_project: str = "prismatic"                                # Name of W&B project (default: `prismatic`)
    wandb_entity: Optional[str] = None                              # Name of W&B entity (default: None)
    #wandb_project: str = "onyx-vlms"
    #wandb_entity: str = "stanford-voltron"

    def __post_init__(self) -> None:
        # init_from_model = 'prism-clip+7b' # "phi-instruct-3+4b+clip" | 'phi-2+3b' | 'prism-dinosiglip+7b' | 'prism-clip+7b'
        self.checkpoint_pt = None
        if self.model.init_from_model is not None:
            # model.repo_id -> "RylanSchaeffer/prismatic-vlms" | "TRI-ML/prismatic-vlms"
            self.checkpoint_pt = hf_hub_download(repo_id=self.model.repo_id, filename=f"{self.model.init_from_model}/checkpoints/latest-checkpoint.pt")
        
        """Set optimization parameters based on `stage` in {"align", "finetune"}."""
        if self.stage == "align":
            self.epochs = self.model.align_epochs
            self.max_steps = self.model.align_max_steps
            self.global_batch_size = self.model.align_global_batch_size
            self.per_device_batch_size = self.model.align_per_device_batch_size

            self.learning_rate = self.model.align_learning_rate
            self.weight_decay = self.model.align_weight_decay
            self.max_grad_norm = self.model.align_max_grad_norm
            self.lr_scheduler_type = self.model.align_lr_scheduler_type
            self.warmup_ratio = self.model.align_warmup_ratio

            self.train_strategy = self.model.align_train_strategy

        elif self.stage.endswith("finetune"):
            self.epochs = self.model.finetune_epochs
            self.max_steps = self.model.finetune_max_steps
            self.global_batch_size = self.model.finetune_global_batch_size
            self.per_device_batch_size = self.model.finetune_per_device_batch_size

            self.learning_rate = self.model.finetune_learning_rate
            self.weight_decay = self.model.finetune_weight_decay
            self.max_grad_norm = self.model.finetune_max_grad_norm
            self.lr_scheduler_type = self.model.finetune_lr_scheduler_type
            self.warmup_ratio = self.model.finetune_warmup_ratio

            self.train_strategy = self.model.finetune_train_strategy

        else:
            raise ValueError(f"Stage `{self.stage}` is not supported!")

    # fmt: on


@draccus.wrap()
def pretrain(cfg: PretrainConfig) -> None:
    overwatch.info("Prismatic VLM Training :: Gathering Light")

    # Note => Under `torchrun` initializing `overwatch` will automatically set up `torch.distributed`
    torch.cuda.set_device(device_id := (overwatch.local_rank()))
    torch.cuda.empty_cache()

    # Create Unique Run Name & Save Directory
    model_id = cfg.model.model_id
    # import pdb; pdb.set_trace()
    
    if (dataset_id := cfg.dataset.dataset_id) == "llava-v15":
        cfg.run_id = f"{model_id}+stage-{cfg.stage}+x{cfg.seed}" if cfg.run_id is None else cfg.run_id
    else:
        cfg.run_id = f"{dataset_id}+{model_id}+stage-{cfg.stage}+x{cfg.seed}" if cfg.run_id is None else cfg.run_id

    # Start =>> Build Directories and Set Randomness
    overwatch.info('"Life is like a prism; what you see depends on how you turn the glass."', ctx_level=1)
    overwatch.info('"Alternatively, life can be viewed like a movie; what you perceive highly depends on the order of events."', ctx_level=1)

    hf_token = cfg.hf_token.read_text().strip() if isinstance(cfg.hf_token, Path) else os.environ[cfg.hf_token]
    worker_init_fn = set_global_seed(cfg.seed, get_worker_init_fn=True)
    os.makedirs(run_dir := (cfg.run_root_dir / cfg.run_id), exist_ok=True)
    os.makedirs(cfg.run_root_dir / cfg.run_id / "checkpoints", exist_ok=True)
    if overwatch.is_rank_zero():
        # Additionally save a JSON version of the config
        draccus.dump(cfg, open(run_dir / "config.yaml", "w"))
        with open(run_dir / "config.yaml", "r") as f_yaml, open(run_dir / "config.json", "w") as f_json:
            yaml_cfg = yaml.safe_load(f_yaml)
            json.dump(yaml_cfg, f_json, indent=2)

    # Load Vision Backbone --> on CPU, in Full Precision (initializing model, image_transform via TIMM)
    overwatch.info(f"Loading Vision Backbone [bold]{cfg.model.vision_backbone_id}[/] via TIMM ")
    vision_backbone, image_transform = get_vision_backbone_and_transform(
        cfg.model.vision_backbone_id, image_resize_strategy=cfg.model.image_resize_strategy, num_frames=cfg.model.num_frames
    )

    # Load LLM Backbone --> on CPU, in Full Precision (initializing Tokenizer + handling special tokens if necessary)
    overwatch.info(f"Loading Pretrained LLM [bold]{cfg.model.llm_backbone_id}[/] via HF Transformers")
    llm_backbone, tokenizer = get_llm_backbone_and_tokenizer(
        cfg.model.llm_backbone_id, llm_max_length=cfg.model.llm_max_length, hf_token=hf_token
    )

    # Create VLM => wraps `vision_backbone` and `llm`
    overwatch.info(f"Instantiating PrismaticVLM `{model_id}` for Training Stage = `{cfg.stage}`")
    # print(cfg.model.enable_mixed_precision_training)
    # import pdb; pdb.set_trace()

    vlm = get_vlm(
        model_id,
        cfg.model.arch_specifier,
        vision_backbone,
        llm_backbone,
        enable_mixed_precision_training=cfg.model.enable_mixed_precision_training,
    )

    if cfg.checkpoint_pt is not None:
        # initialize the VLM from pretrained checkpoint
        overwatch.info(f"Initializing from checkpoint path: {cfg.checkpoint_pt}")
        model_state_dict = torch.load(cfg.checkpoint_pt, map_location="cpu")["model"]
        # import pdb; pdb.set_trace()
        assert (
            "projector" in model_state_dict and "llm_backbone" in model_state_dict
        ), "PrismaticVLM `from_pretrained` expects checkpoint with keys for `projector` AND `llm_backbone`!"

        vlm.projector.load_state_dict(model_state_dict["projector"])
        vlm.llm_backbone.load_state_dict(model_state_dict["llm_backbone"])
    
    # print(vlm.projector.type)
    # print(vlm.llm_backbone.type)
    # print(vlm.vision_backbone.dtype)  -> torch.bfloat16
    # import pdb; pdb.set_trace()

    # [TODO] testing first pass of the model with video inputs..
    # vlm.requires_grad_(False)
    # vlm.eval()
    # device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    # vlm.to(device, dtype=torch.bfloat16)
    # vlm.eval()
    # image = []
    # for i in range(8):
    #     filepath = f"webvid/10006322/{str(i).zfill(4)}.png"
    #     image.append(Image.open(filepath).convert("RGB"))
    # # prompt_text = "<image>\nDescribe what is happening in the video."
    # prompt_text = "Input: What is going on in the image?\nOutput:"  # Input: <prompt>\nOutput:
    # image = image[:7]
    # generated_text = vlm.generate(
    #                     image,
    #                     prompt_text,
    #                     do_sample=False,
    #                     temperature=1.0,
    #                     max_new_tokens=512,
    #                     min_length=1
    #                 )
    # import pdb; pdb.set_trace()

    # [Explicit] Call to `freeze_backbones` here for clarity => will log exactly what is frozen / what's not!
    overwatch.info(f"Invoking `VLM.freeze_backbones()` for `{model_id}` => Training Stage: `{cfg.stage}`")
    vlm.freeze_backbones(cfg.stage)

    # Load Weights from Checkpoint (depends on stage, config) -> this is for the projector! (for no-align, model is not loaded from a checkpoint)
    overwatch.info(f"Invoking `VLM.load_checkpoint()` for `{model_id}` => Training Stage: `{cfg.stage}`")
    vlm.load_from_checkpoint(cfg.stage, run_dir, pretrained_checkpoint=cfg.pretrained_checkpoint)

    # Get Dataset for Specified Stage
    overwatch.info(f"Creating Dataset `{cfg.dataset.dataset_id}` => Stage: `{cfg.stage}`")
    train_dataset, collator = get_dataset_and_collator(
        cfg.stage,
        cfg.dataset,
        image_transform,
        tokenizer,
        prompt_builder_fn=llm_backbone.prompt_builder_fn,
        default_image_resolution=vision_backbone.default_image_resolution,
        padding_side=tokenizer.padding_side,
    )

    val_dataset, val_collator = get_dataset_and_collator( # val_collator not used currently
        cfg.stage,
        cfg.val_dataset,
        image_transform,
        tokenizer,
        prompt_builder_fn=llm_backbone.prompt_builder_fn,
        default_image_resolution=vision_backbone.default_image_resolution,
        padding_side=tokenizer.padding_side,
    )

    # Create Train Strategy
    overwatch.info(f"Initializing Train Strategy `{cfg.train_strategy}`")
    train_strategy = get_train_strategy(
        train_strategy=cfg.train_strategy,
        vlm=vlm,
        device_id=device_id,
        epochs=cfg.epochs,
        max_steps=cfg.max_steps,
        global_batch_size=cfg.global_batch_size,
        per_device_batch_size=cfg.per_device_batch_size,
        learning_rate=cfg.learning_rate,
        weight_decay=cfg.weight_decay,
        max_grad_norm=cfg.max_grad_norm,
        lr_scheduler_type=cfg.lr_scheduler_type,
        warmup_ratio=cfg.warmup_ratio,
        enable_gradient_checkpointing=cfg.model.enable_gradient_checkpointing,
        enable_mixed_precision_training=cfg.model.enable_mixed_precision_training,
        reduce_in_full_precision=cfg.model.reduce_in_full_precision,
        worker_init_fn=worker_init_fn,
    )
    train_strategy.run_setup(run_dir=run_dir, n_train_examples=len(train_dataset))

    # Create Metrics =>> Handles on the fly tracking, logging to specified trackers (e.g., JSONL, Weights & Biases)
    overwatch.info(f"Creating Metrics with Active Trackers => `{cfg.trackers}`")
    metrics = Metrics(
        cfg.trackers,
        cfg.run_id,
        run_dir,
        draccus.encode(cfg),
        cfg.stage,
        wandb_project=cfg.wandb_project,
        wandb_entity=cfg.wandb_entity,
        grad_accumulation_steps=train_strategy.grad_accumulation_steps,
    )


    # [FIXME] temp fix. fully-sharded data parallel on multi-gpus throws an error: `ValueError: Requires uniform dtype across all gradients but got {torch.bfloat16, torch.float32}`
    # https://github.com/huggingface/accelerate/issues/1620#issuecomment-2407102051
    for name, module in vlm.named_modules():
        try:
            module.to(torch.bfloat16)
        except Exception as e:
            pass

    # Run Training
    overwatch.info("Starting Training Loop")
    train_strategy.run_training(train_dataset, val_dataset, collator, metrics, stage=cfg.stage, seed=cfg.seed)

    # Finalize
    overwatch.info("Done with Training =>> Finalizing Metrics")
    metrics.finalize()

    # And... we're done!
    overwatch.info("... and that's all, folks!")
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    pretrain()