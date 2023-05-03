from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

import evaluate
import numpy as np
import torch
import torch.nn as nn
from datasets import load_dataset
from peft import LoraConfig, TaskType, get_peft_model, PeftModel
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    LlamaForCausalLM, 
    LlamaTokenizer,
    HfArgumentParser,
    PreTrainedTokenizerBase,
    Trainer,
    TrainingArguments,
)
from transformers.utils import PaddingStrategy


DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "</s>"
DEFAULT_UNK_TOKEN = "</s>"


# Define and parse arguments.
@dataclass
class ScriptArguments:
    """
    These arguments vary depending on how many GPUs you have, what their capacity and features are, and what size model you want to train.
    """

    local_rank: Optional[int] = field(default=-1, metadata={"help": "Used for multi-gpu"})
    resume_from_checkpoint: Optional[bool] = field(
        default=False,
        metadata={"help": "If you want to resume training where it left off."},
    )
    deepspeed: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to deepspeed config if using deepspeed. You may need this if the model that you want to train doesn't fit on a single GPU."
        },
    )
    per_device_train_batch_size: Optional[int] = field(default=4)
    per_device_eval_batch_size: Optional[int] = field(default=1)
    gradient_accumulation_steps: Optional[int] = field(default=1)
    learning_rate: Optional[float] = field(default=2e-5)
    weight_decay: Optional[int] = field(default=0.001)
    model_name: Optional[str] = field(
        default="decapoda-research/llama-7b-hf",        ### Changed to Llama-7b-hf, the model which we want to use as base model.
        metadata={
            "help": "The model that you want to train from the Hugging Face hub. E.g. gpt2, gpt2-xl, bert, etc."
        },
    )
    bf16: Optional[bool] = field(
        default=True,
        metadata={
            "help": "This essentially cuts the training time in half if you want to sacrifice a little precision and have a supported GPU."
        },
    )
    num_train_epochs: Optional[int] = field(
        default=1,
        metadata={"help": "The number of training epochs for the reward model."},
    )
    ## NOTE: CHANGE DEFAULT BACK TO 100 000 WHEN DONE TESTING
    train_subset: Optional[int] = field(
        default=1000,
        metadata={"help": "The size of the subset of the training data to use"},
    )
    ## NOTE: CHANGE DEFAULT BACK TO 50 000 WHEN DONE TESTING
    eval_subset: Optional[int] = field(
        default=500,
        metadata={"help": "The size of the subset of the eval data to use"},
    )
    gradient_checkpointing: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables gradient checkpointing."},
    )
    optim: Optional[str] = field(
        default="adamw_hf",
        metadata={"help": "Enables gradient checkpointing."},
    )
    lr_scheduler_type: Optional[str] = field(
        default="linear",
        metadata={"help": "The lr scheduler"},
    )


parser = HfArgumentParser(ScriptArguments)
script_args = parser.parse_args_into_dataclasses()[0]

# Load the human stack-exchange-paired dataset for tuning the reward model.
#train_dataset = load_dataset("samhog/stack-exchange-mini", data_dir="data/reward", split="train[:0.1%]")
train_dataset = load_dataset("samhog/stack-exchange-mini", data_dir="data/reward", split="train[:10%]")
if script_args.train_subset > 0:
    train_dataset = train_dataset.select(range(script_args.train_subset))
#eval_dataset = load_dataset("samhog/stack-exchange-mini", data_dir="data/evaluation", split="train[:0.1%]")
eval_dataset = load_dataset("samhog/stack-exchange-mini", data_dir="data/evaluation", split="train[:10%]")
if script_args.eval_subset > 0:
    eval_dataset = eval_dataset.select(range(script_args.eval_subset))

"""
## Using only a small sample of the total dataset in samhog/stack-exchange-mini
percentage = 0.01
num_examples = int(len(train_dataset) * percentage)
train_dataset = train_dataset.select(range(num_examples))
"""
print(train_dataset)
print("Dataset cropping done")


# Define the training args. Needs to be done before the model is loaded if you are using deepspeed.
"""
model_name_split = script_args.model_name.split("/")[-1]
output_name = (
    f"{model_name_split}_peft_stack-exchange-paired_rmts__{script_args.train_subset}_{script_args.learning_rate}"
)
"""
output_name = "lora-alpaca" # Just the same as the fine-tuning implementation (https://colab.research.google.com/drive/11m4444w5KOtio3x-atLevdNDGdfFlwoj#scrollTo=JCB9UzMVwsSM)
training_args = TrainingArguments(
    output_dir=output_name,
    learning_rate=script_args.learning_rate,
    per_device_train_batch_size=script_args.per_device_train_batch_size,
    per_device_eval_batch_size=script_args.per_device_eval_batch_size,
    num_train_epochs=script_args.num_train_epochs,
    weight_decay=script_args.weight_decay,
    evaluation_strategy="steps",
    eval_steps=500,
    save_strategy="steps",
    save_steps=500,
    gradient_accumulation_steps=script_args.gradient_accumulation_steps,
    gradient_checkpointing=script_args.gradient_checkpointing,
    deepspeed=script_args.deepspeed,
    local_rank=script_args.local_rank,
    remove_unused_columns=False,
    label_names=[],
    #bf16=script_args.bf16,
    fp16=True,
    logging_strategy="steps",
    logging_steps=10,
    optim=script_args.optim,
    lr_scheduler_type=script_args.lr_scheduler_type,
)
# Load the value-head model and tokenizer. ## CHANGED TO LlamaTokenizer FROM AutoTokenizer
tokenizer = LlamaTokenizer.from_pretrained(script_args.model_name, use_auth_token=True)
config = AutoConfig.from_pretrained(script_args.model_name)

if "llama" in script_args.model_name:
    # required for llama
    tokenizer.add_special_tokens(
        {
            "eos_token": DEFAULT_EOS_TOKEN,
            "bos_token": DEFAULT_BOS_TOKEN,
            "unk_token": DEFAULT_UNK_TOKEN,
            "pad_token": DEFAULT_PAD_TOKEN,
        }
    )
else:
    # required for gpt2
    tokenizer.pad_token = tokenizer.eos_token

peft_config = LoraConfig(
    task_type=TaskType.SEQ_CLS,
    inference_mode=False,
    r=8,
    lora_alpha=32,
    lora_dropout=0.1,
)

## Added "load_in_8bit=True, might not work, but we'll probably need it. CHANGED TO LlamaForCausalLM FROM AutoModelForSequenceClassification
## And set torch_dtype=torch.float16 INSTEAD OF bfloat16. CHANGE THIS BACK IF USING A100 (I THINK)
## And set device_map="auto" SINCE IT IS NEEDED FOR 8BIT LOADING
model = LlamaForCausalLM.from_pretrained(
    script_args.model_name, num_labels=1, device_map="auto", torch_dtype=torch.float16, load_in_8bit=True
)
model = get_peft_model(model, peft_config)
model = PeftModel.from_pretrained(model, "samhog/psychology-alpaca")    # Loading psychology-alpaca in the same way as the generator script on colab
model.print_trainable_parameters()

# Need to do this for gpt2, because it doesn't have an official pad token.
tokenizer.pad_token = tokenizer.eos_token
model.config.pad_token_id = tokenizer.eos_token_id
model.config.use_cache = not script_args.gradient_checkpointing
num_proc = 24  # Can adjust to be higher if you have more processors.
original_columns = train_dataset.column_names


# Turn the dataset into pairs of post + summaries, where text_j is the preferred question + answer and text_k is the other.
# Then tokenize the dataset.
def preprocess_function(examples):
    new_examples = {
        "input_ids_j": [],
        "attention_mask_j": [],
        "input_ids_k": [],
        "attention_mask_k": [],
    }
    for question, response_j, response_k in zip(examples["question"], examples["response_j"], examples["response_k"]):
        tokenized_j = tokenizer("Question: " + question + "\n\nAnswer: " + response_j, truncation=True)
        tokenized_k = tokenizer("Question: " + question + "\n\nAnswer: " + response_k, truncation=True)

        new_examples["input_ids_j"].append(tokenized_j["input_ids"])
        new_examples["attention_mask_j"].append(tokenized_j["attention_mask"])
        new_examples["input_ids_k"].append(tokenized_k["input_ids"])
        new_examples["attention_mask_k"].append(tokenized_k["attention_mask"])

    return new_examples


# preprocess the dataset and filter out QAs that are longer than 512
train_dataset = train_dataset.map(
    preprocess_function, batched=True, num_proc=num_proc, remove_columns=original_columns
)
train_dataset = train_dataset.filter(lambda x: len(x["input_ids_j"]) <= 512 and len(x["input_ids_k"]) <= 512)

eval_dataset = eval_dataset.map(preprocess_function, batched=True, num_proc=num_proc, remove_columns=original_columns)
eval_dataset = eval_dataset.filter(lambda x: len(x["input_ids_j"]) <= 512 and len(x["input_ids_k"]) <= 512)


# We need to define a special data collator that batches the data in our j vs k format.
@dataclass
class RewardDataCollatorWithPadding:
    tokenizer: PreTrainedTokenizerBase
    padding: Union[bool, str, PaddingStrategy] = True
    max_length: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    return_tensors: str = "pt"

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, Any]:
        features_j = []
        features_k = []
        for feature in features:
            features_j.append(
                {
                    "input_ids": feature["input_ids_j"],
                    "attention_mask": feature["attention_mask_j"],
                }
            )
            features_k.append(
                {
                    "input_ids": feature["input_ids_k"],
                    "attention_mask": feature["attention_mask_k"],
                }
            )
        batch_j = self.tokenizer.pad(
            features_j,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors=self.return_tensors,
        )
        batch_k = self.tokenizer.pad(
            features_k,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors=self.return_tensors,
        )
        batch = {
            "input_ids_j": batch_j["input_ids"],
            "attention_mask_j": batch_j["attention_mask"],
            "input_ids_k": batch_k["input_ids"],
            "attention_mask_k": batch_k["attention_mask"],
            "return_loss": True,
        }
        return batch


# Define the metric that we'll use for validation.
accuracy = evaluate.load("accuracy")


def compute_metrics(eval_pred):
    predictions, _ = eval_pred
    # Here, predictions is rewards_j and rewards_k.
    # We want to see how much of the time rewards_j > rewards_k.
    predictions = np.argmax(predictions, axis=0)
    labels = np.zeros(predictions.shape)
    return accuracy.compute(predictions=predictions, references=labels)


class RewardTrainer(Trainer):
    # Define how to compute the reward loss. We use the InstructGPT pairwise logloss: https://arxiv.org/abs/2203.02155
    def compute_loss(self, model, inputs, return_outputs=False):
        rewards_j = model(input_ids=inputs["input_ids_j"], attention_mask=inputs["attention_mask_j"])[0]
        rewards_k = model(input_ids=inputs["input_ids_k"], attention_mask=inputs["attention_mask_k"])[0]
        loss = -nn.functional.logsigmoid(rewards_j - rewards_k).mean()
        if return_outputs:
            return loss, {"rewards_j": rewards_j, "rewards_k": rewards_k}
        return loss


# Train the model, woohoo.
trainer = RewardTrainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    compute_metrics=compute_metrics,
    data_collator=RewardDataCollatorWithPadding(tokenizer=tokenizer, max_length=512),
)

trainer.train(script_args.resume_from_checkpoint)

print("Saving last checkpoint of the model")
model.save_pretrained(output_name + "_peft_last_checkpoint")
model.push_to_hub("samhog/psychology-alpaca-rm", use_auth_token=True)
