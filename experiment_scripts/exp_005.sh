# '''
# total input frames = 16, videos per cluster = 4
# CLIP-ViT@336 + llama-2
# '''

torchrun --standalone --nnodes 1 --nproc-per-node 2 scripts/pretrain.py \
  --model.type "one-stage+7b" \
  --model.model_id "prism-clip+7b-webvid-train-45k-total-input-frames=16-videos-per-cluster=4-epochs=5-gpus=2-005" \
  --dataset.type "webvid" \
  --model.image_resize_strategy "resize-naive" \
  --model.llm_backbone_id "llama2-7b-pure" \
  --model.vision_backbone_id "video-clip-vit-l-336px" \
  --model.arch_specifier "no-align+gelu-mlp" \
  --model.finetune_global_batch_size 2 \
  --model.finetune_per_device_batch_size 1 \
  --model.num_frames 16 \
  --model.rope_scaling_factor 3.0 \
  --model.init_from_model "prism-clip+7b" \
  --model.repo_id "TRI-ML/prismatic-vlms" \
  --model.finetune_epochs 5 \
  --dataset.type "webvid" \
  --dataset.finetune_stage_components '["/vision/u/silsingh/prismatic-vlms/dataset_splits/webvid_train_45k_videos_per_cluster=4_total_input_frames=16.json", "/vision/u/silsingh/prismatic-vlms/webvid_num_frames=4"]' \
  --val_dataset.type "webvid_val" \
  --val_dataset.finetune_stage_components '["/vision/u/silsingh/prismatic-vlms/dataset_splits/webvid_val_5k_videos_per_cluster=4_total_input_frames=16.json", "/vision/u/silsingh/prismatic-vlms/webvid_num_frames=4"]' \
  