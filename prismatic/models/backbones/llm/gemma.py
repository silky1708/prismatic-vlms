"""
llama.py

Class definition for all LLMs derived from LlamaForCausalLM.
"""

from typing import Optional, Type, Dict

import torch
from torch import nn as nn
from transformers import GemmaForCausalLM
from transformers.models.gemma.modeling_gemma import GemmaDecoderLayer

from prismatic.models.backbones.llm.base_llm import HFCausalLLMBackbone
from prismatic.models.backbones.llm.prompting import (
    GemmaChatPromptBuilder,
    PromptBuilder,
    PurePromptBuilder,
)

# Registry =>> Support Llama-2 Models (from HF Transformers)
# fmt: off
GEMMA_MODELS = {
    # === Google Gemma Instruction-Tuned ===
    "gemma-2b": {
        "llm_family": "gemma", "llm_cls": GemmaForCausalLM, "hf_hub_path": "google/gemma-2b"
    },

    "gemma-7b": {
        "llm_family": "gemma", "llm_cls": GemmaForCausalLM, "hf_hub_path": "google/gemma-7b"
    },

    # === Google Gemma Instruction-Tuned ===
    "gemma-2b-instruct": {
        "llm_family": "gemma", "llm_cls": GemmaForCausalLM, "hf_hub_path": "google/gemma-2b-it"
    },

    "gemma-8b-instruct": {
        "llm_family": "gemma", "llm_cls": GemmaForCausalLM, "hf_hub_path": "google/gemma-7b-it"
    },
}
# fmt: on


class GemmaLLMBackbone(HFCausalLLMBackbone):
    def __init__(
        self,
        llm_backbone_id: str,
        llm_max_length: int = 2048,
        hf_token: Optional[str] = None,
        inference_mode: bool = False,
        use_flash_attention_2: bool = True,
        rope_kwargs: Optional[Dict] = None
    ) -> None:
        super().__init__(
            llm_backbone_id,
            llm_max_length=llm_max_length,
            hf_token=hf_token,
            inference_mode=inference_mode,
            use_flash_attention_2=use_flash_attention_2,
            rope_kwargs=rope_kwargs,
            **GEMMA_MODELS[llm_backbone_id],
        )

        # [Special Case] Llama-2 PAD Token Handling --> for clarity, we add an extra token (and resize)
        self.tokenizer.add_special_tokens({"pad_token": "<PAD>"})
        self.llm.config.pad_token_id = self.tokenizer.pad_token_id
        self.llm.resize_token_embeddings(len(self.tokenizer), pad_to_multiple_of=64)

    @property
    def prompt_builder_fn(self) -> Type[PromptBuilder]:
        if self.identifier.startswith("gemma-") and not self.identifier.endswith("-it"):
            return PurePromptBuilder

        elif self.identifier.startswith("gemma-") and self.identifier.endswith("-it"):
            return GemmaChatPromptBuilder

        raise ValueError(f"No PromptBuilder defined for LLM Backbone `{self.identifier}`")

    @property
    def transformer_layer_cls(self) -> Type[nn.Module]:
        return GemmaDecoderLayer

    @property
    def half_precision_dtype(self) -> torch.dtype:
        """Llama-2 was trained in BF16; see https://huggingface.co/docs/transformers/main/model_doc/llama2."""
        return torch.bfloat16
