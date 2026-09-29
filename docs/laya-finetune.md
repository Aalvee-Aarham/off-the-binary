# Fine-tuning Laya for fuel routing

**Goal.** Make the Laya System-One model useful for the router's 4 questions (`regime`, `algorithm`, `severity`,
`needs_human` in `backend/app/router.py::QUESTIONS`) by fine-tuning it on labels we generate on the digital twin,
and measure whether it helped.

__RESULTS__

## 1. The official method (found, and used)

Laya's authors publish the fine-tuning loop as
[`notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb`](https://github.com/NandhaKishorM/laya/blob/main/notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb)
(linked from the PyPI README, *Fine-Tuning* section). It is **RLCD**: for every (state, question) sequence the model
outputs one logit per option; training samples 4 noisy logit vectors (sigma 0.4 -> 0.1), rewards each with a
strictly proper scoring rule (`laya.common.proper_reward`: log + 0.75 x spherical score, minus RPS for `score`
questions), takes a GRPO-style policy gradient, and adds a soft cross-entropy to the gold distribution. AdamW with
encoder lr 2.5e-5 / head lr 1e-4, cosine schedule, micro-batch 8 x grad-accum 4, DDP over 2 T4s, fp16 autocast,
gradient checkpointing. ~10% of items (max 400) are held out and used to fit one temperature per question type
(LBFGS); `temperature_by_options` is dropped from the config so the new fit is not masked. Output layout =
a published checkpoint (`model.safetensors`, `rl_agent_config.json`, `encoder/`, `tokenizer/`), loadable by
`laya.Agent(dir)`.

It works for our question types as-is: the notebook's dataset (`LocalLLaMA/typed-decisions`) uses exactly
`choice`/`score`/`noul` with gold `probabilities` per option, so our data is written in that row format and the
loss, calibration and export are the notebook's code (factored into `ml/finetune/rlcd.py`).

## 2. Dataset (`ml/finetune/make_dataset.py`)

__DATASET__

## 3. What was run here (CPU) and what is left for a GPU

__RUNS__

## 4. Commands

```bash
# 1. data (backend env; ~28 min on 4 cores for 5,000 states)
cd backend && ../.venv/Scripts/python ../ml/finetune/make_dataset.py --n 5000 --workers 4
#    only the 24 router_eval situations (after a twin change):  ... make_dataset.py --router-eval-only

# 2a. GPU (recommended): Kaggle / Colab, see ml/finetune/finetune.ipynb (first cell has the upload steps)
#     or on any CUDA box:
cd ml/finetune && torchrun --standalone --nproc_per_node=<gpus> train_gpu.py --data data --out checkpoints/laya-fuel
python evaluate.py --ckpt typed-decisions --device cuda --out results/eval_zero_shot.json
python evaluate.py --ckpt checkpoints/laya-fuel --device cuda --out results/eval_fine_tuned.json

# 2b. CPU head-only (what produced the numbers above), .venv-ml
.venv-ml/Scripts/python ml/finetune/cpu_head_finetune.py features --split train --limit 600
.venv-ml/Scripts/python ml/finetune/cpu_head_finetune.py features --split test --limit 250
.venv-ml/Scripts/python ml/finetune/cpu_head_finetune.py features --split router_eval
.venv-ml/Scripts/python ml/finetune/cpu_head_finetune.py train --epochs 4
.venv-ml/Scripts/python ml/finetune/cpu_head_finetune.py eval       # -> ml/finetune/results/cpu_head_eval.json

# 3. serve a fine-tuned dir locally and run the existing harness against it (CLI unchanged)
LAYA_FINETUNED_DIR=ml/finetune/checkpoints/laya-fuel-cpu-head LAYA_PORT=8001 LAYA_MODELS=typed-decisions \
  LAYA_MAX_LOADED=1 LAYA_THREADS=4 .venv-ml/Scripts/python ml/finetune/serve_finetuned.py
cd backend && ../.venv/Scripts/python -m scripts.router_eval --laya http://localhost:8001 --jev-key "" \
  --out ../ml/finetune/results/router_eval_finetuned_http.json
```

## 5. Serving the fine-tuned checkpoint in `ml-service`

`laya-serve` only knows the three Hub checkpoints and has no "local dir" variable. `ml/finetune/serve_finetuned.py`
(10 lines) re-points one name (`typed-decisions` by default) in `laya.router.DEFAULT_MODELS` at a local directory
before the server builds its Router, then runs the unmodified `laya.serve.main()`. The backend keeps sending
`model="typed-decisions"` (`LAYA_MODEL`), so **no backend change**. Without `LAYA_FINETUNED_DIR` it *is* `laya-serve`.

The Dockerfile is **not changed**. In `docker-compose.yml` (or an override file) give `ml-service`:

```yaml
  ml-service:
    build: ./ml
    environment:
      LAYA_MODELS: typed-decisions
      LAYA_FINETUNED_DIR: /models/laya-fuel          # unzip laya-fuel.zip here
    volumes:
      - laya-models:/models
      - ./ml/finetune/checkpoints/laya-fuel:/models/laya-fuel:ro
      - ./ml/finetune/serve_finetuned.py:/opt/serve_finetuned.py:ro
    command: ["python", "/opt/serve_finetuned.py"]
```

Same memory as today (identical architecture, ~1.7 GB resident in fp32; `mem_limit: 3g` is fine), same latency,
same `/health` and `/v1/systemone`. If you would rather bake it in, the equivalent two-line Dockerfile tweak is
`COPY finetune/serve_finetuned.py /opt/` + `CMD ["python", "/opt/serve_finetuned.py"]` with the checkpoint still
mounted (it is 0.8 GB; keep it out of the image and out of git -- `ml/finetune/.gitignore` ignores `checkpoints/`).
Rollback = drop `LAYA_FINETUNED_DIR`.

## 6. Caveats

__CAVEATS__

## 7. Files

| path | what |
|---|---|
| `ml/finetune/make_dataset.py` | twin data generator (backend `.venv`) |
| `ml/finetune/data/{train,test,router_eval}.jsonl`, `meta.json` | generated data (the repo `.gitignore` ignores `data/`: regenerate, or `git add -f`) |
| `ml/finetune/rlcd.py` | the official recipe's item builder, RLCD loss, temperature fit, checkpoint writer + router_eval metrics |
| `ml/finetune/train_gpu.py` | official DDP training script on our data (full fine-tune) |
| `ml/finetune/finetune.ipynb` | Kaggle/Colab notebook: zero-shot eval -> train -> eval -> zip |
| `ml/finetune/evaluate.py` | score any checkpoint through `laya.Agent` (the serving path) |
| `ml/finetune/cpu_head_finetune.py` | CPU head-only variant (cached encoder features) |
| `ml/finetune/serve_finetuned.py` | `laya-serve` with a local fine-tuned checkpoint |
| `ml/finetune/results/*.json` | measured results |
| `ml/finetune/checkpoints/` | local checkpoints (git-ignored) |
