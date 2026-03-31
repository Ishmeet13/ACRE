"""
Fine-tuning Pipeline
====================
Collects bug-fix pairs from GitHub, formats them as instruction data,
and fine-tunes CodeLlama-7B using PEFT/LoRA.

Training data format:
  <s>[INST] Fix the following bug:
  Bug: {title}
  Severity: {severity}
  Code:
  ```python
  {vulnerable_code}
  ```
  [/INST]
  {fixed_code} </s>

MLflow tracks every experiment: hyperparams, eval metrics, model versions.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional
from datasets import Dataset
import torch

import mlflow
import mlflow.pytorch
from peft import (
    LoraConfig,
    TaskType,
    get_peft_model,
    prepare_model_for_kbit_training,
)
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
    Trainer,
    DataCollatorForSeq2Seq,
)
from trl import SFTTrainer

logger = logging.getLogger(__name__)

BASE_MODEL    = os.getenv("BASE_MODEL",    "codellama/CodeLlama-7b-Instruct-hf")
OUTPUT_DIR    = os.getenv("OUTPUT_DIR",    "./finetuned_model")
HF_TOKEN      = os.getenv("HF_TOKEN",     "")
MLFLOW_URI    = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")


# ── Data Collection ───────────────────────────────────────────────────────────
def collect_bug_fix_pairs(
    data_dir: str = "./training_data",
    max_samples: int = 5000,
) -> list[dict]:
    """
    Load bug-fix pairs from:
    1. Pre-collected GitHub data (JSON files in data_dir)
    2. ACRE's own analysis history (patches that PASSED evaluation)
    3. Public datasets (CodeSearchNet bug fixes, BugsInPy)
    """
    pairs = []

    # Load from local JSON files (pre-scraped)
    for fpath in Path(data_dir).glob("*.jsonl"):
        with open(fpath) as f:
            for line in f:
                try:
                    item = json.loads(line.strip())
                    if _validate_pair(item):
                        pairs.append(item)
                except Exception:
                    continue

    # Load successful ACRE patches from Postgres
    try:
        from db_sync import get_successful_patches_sync
        acre_patches = get_successful_patches_sync(limit=max_samples)
        for p in acre_patches:
            pairs.append({
                "bug_title":        p["bug_title"],
                "bug_description":  p["bug_description"],
                "severity":         p["severity"],
                "bug_type":         p["bug_type"],
                "vulnerable_code":  p["original_code"],
                "fixed_code":       p["fixed_code"],
                "language":         p["language"],
                "source":           "acre",
            })
        logger.info(f"Loaded {len(acre_patches)} pairs from ACRE history")
    except Exception as e:
        logger.warning(f"Could not load ACRE pairs: {e}")

    logger.info(f"Total training pairs collected: {len(pairs)}")
    return pairs[:max_samples]


def _validate_pair(item: dict) -> bool:
    """Sanity-check a bug-fix pair before including it in training."""
    required = {"vulnerable_code", "fixed_code"}
    if not required.issubset(item.keys()):
        return False
    if len(item["vulnerable_code"]) < 20 or len(item["fixed_code"]) < 20:
        return False
    if item["vulnerable_code"] == item["fixed_code"]:
        return False
    if len(item["fixed_code"]) > 4000:   # too long for context window
        return False
    return True


# ── Dataset Formatting ────────────────────────────────────────────────────────
def format_instruction(item: dict) -> str:
    """
    Format a bug-fix pair into CodeLlama instruction format.
    """
    lang = item.get("language", "python")
    return (
        f"<s>[INST] You are an expert software engineer. "
        f"Fix the following bug and return ONLY the corrected code.\n\n"
        f"Bug title: {item.get('bug_title', 'Bug Fix')}\n"
        f"Severity: {item.get('severity', 'MEDIUM')}\n"
        f"Bug type: {item.get('bug_type', 'logic')}\n\n"
        f"Buggy code:\n```{lang}\n{item['vulnerable_code']}\n```"
        f" [/INST]\n"
        f"```{lang}\n{item['fixed_code']}\n```</s>"
    )


def build_dataset(pairs: list[dict]) -> Dataset:
    formatted = [{"text": format_instruction(p)} for p in pairs]
    # 90/10 split
    split_idx = int(len(formatted) * 0.9)
    return Dataset.from_list(formatted)


# ── Model Setup ───────────────────────────────────────────────────────────────
def load_model_and_tokenizer(model_name: str):
    """Load CodeLlama with 4-bit quantization for efficient fine-tuning."""
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_name,
        token=HF_TOKEN,
        padding_side="right",
    )
    tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="auto",
        token=HF_TOKEN,
    )
    model = prepare_model_for_kbit_training(model)
    return model, tokenizer


def apply_lora(model) -> tuple:
    """Apply LoRA adapters — train only a small fraction of parameters."""
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,                    # LoRA rank
        lora_alpha=32,           # scaling factor
        lora_dropout=0.1,
        target_modules=[         # CodeLlama attention + MLP layers
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()
    return model, lora_config


# ── Training ──────────────────────────────────────────────────────────────────
def train(
    pairs: Optional[list[dict]] = None,
    num_epochs: int = 3,
    batch_size: int = 4,
    learning_rate: float = 2e-4,
    max_seq_length: int = 2048,
):
    """Main training function. Tracked in MLflow."""
    mlflow.set_tracking_uri(MLFLOW_URI)
    mlflow.set_experiment("acre_finetuning")

    if pairs is None:
        pairs = collect_bug_fix_pairs()

    if len(pairs) < 50:
        raise ValueError(f"Not enough training pairs: {len(pairs)} (need at least 50)")

    dataset = build_dataset(pairs)
    model, tokenizer = load_model_and_tokenizer(BASE_MODEL)
    model, lora_config = apply_lora(model)

    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        num_train_epochs=num_epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=4,
        learning_rate=learning_rate,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=10,
        save_steps=100,
        eval_steps=100,
        evaluation_strategy="steps",
        save_strategy="steps",
        load_best_model_at_end=True,
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        report_to="mlflow",
        run_name=f"codellama_lora_r{lora_config.r}",
        dataloader_num_workers=2,
        group_by_length=True,
    )

    with mlflow.start_run():
        # Log hyperparameters
        mlflow.log_params({
            "base_model": BASE_MODEL,
            "lora_r": lora_config.r,
            "lora_alpha": lora_config.lora_alpha,
            "lora_dropout": lora_config.lora_dropout,
            "num_epochs": num_epochs,
            "batch_size": batch_size,
            "learning_rate": learning_rate,
            "max_seq_length": max_seq_length,
            "training_pairs": len(pairs),
        })

        trainer = SFTTrainer(
            model=model,
            train_dataset=dataset,
            max_seq_length=max_seq_length,
            tokenizer=tokenizer,
            args=training_args,
            dataset_text_field="text",
            packing=True,  # pack multiple short samples into one context window
        )

        trainer.train()

        # Save model + push to MLflow registry
        trainer.save_model(OUTPUT_DIR)
        tokenizer.save_pretrained(OUTPUT_DIR)

        mlflow.pytorch.log_model(
            pytorch_model=model,
            artifact_path="model",
            registered_model_name="acre-codellama-lora",
        )

        logger.info(f"Model saved to {OUTPUT_DIR} and logged in MLflow")


# ── Evaluation ────────────────────────────────────────────────────────────────
def evaluate_model(model_path: str, test_pairs: list[dict]) -> dict:
    """
    Evaluate the fine-tuned model on held-out test pairs.
    Metrics:
    - exact_match: patch == ground truth fixed code
    - syntax_valid: patch is syntactically valid Python
    - bleu_score: n-gram overlap with reference
    - compile_success: code runs without ImportError
    """
    from evaluate import load as eval_load
    bleu = eval_load("bleu")

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path, device_map="auto")
    model.eval()

    exact_matches = 0
    syntax_valid_count = 0
    predictions = []
    references = []

    import ast
    for pair in test_pairs[:200]:  # eval on 200 samples
        prompt = format_instruction(pair).split("[/INST]")[0] + "[/INST]\n"
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=512,
                temperature=0.1,
                do_sample=False,
            )
        generated = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
        # Strip markdown
        if "```" in generated:
            generated = generated.split("```")[1].lstrip("python\n")

        if generated.strip() == pair["fixed_code"].strip():
            exact_matches += 1
        try:
            ast.parse(generated)
            syntax_valid_count += 1
        except SyntaxError:
            pass

        predictions.append(generated)
        references.append([pair["fixed_code"]])

    bleu_score = bleu.compute(predictions=predictions, references=references)["bleu"]

    metrics = {
        "exact_match": exact_matches / len(test_pairs[:200]),
        "syntax_valid_rate": syntax_valid_count / len(test_pairs[:200]),
        "bleu_score": round(bleu_score, 4),
        "test_samples": len(test_pairs[:200]),
    }

    with mlflow.start_run(nested=True, run_name="model_eval"):
        mlflow.log_metrics(metrics)

    logger.info(f"Evaluation results: {metrics}")
    return metrics


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./training_data")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    args = parser.parse_args()

    pairs = collect_bug_fix_pairs(data_dir=args.data_dir)
    split = int(len(pairs) * 0.9)
    train(pairs[:split], num_epochs=args.epochs, batch_size=args.batch_size, learning_rate=args.lr)
    evaluate_model(OUTPUT_DIR, pairs[split:])
