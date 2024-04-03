import dataclasses
import logging
import os
from dataclasses import dataclass, field
from typing import Optional, Union

import jax.random as jrandom
from transformers import PretrainedConfig as HfConfig  # noqa

import haliax as hax
from haliax import Axis
from haliax.partitioning import named_jit, round_axis_for_partitioning

import levanter
from levanter import callbacks
from levanter.compat.hf_checkpoints import HFCompatConfig, save_hf_checkpoint_callback
from levanter.data.audio import AudioIODatasetConfig, AudioTextDataset
from levanter.models.asr_model import ASRConfig
from levanter.models.via import ViaASRModel, ViaConfig, connector_only
from levanter.models.whisper import WhisperConfig
from levanter.optim import AdamConfig, OptimizerConfig
from levanter.trainer import Trainer, TrainerConfig
from levanter.utils.jax_utils import parameter_count


logger = logging.getLogger(__name__)


@dataclass
class TrainASRConfig:
    data: AudioIODatasetConfig = field(default_factory=AudioIODatasetConfig)
    trainer: TrainerConfig = field(default_factory=TrainerConfig)
    model: ASRConfig = field(default_factory=WhisperConfig)
    optimizer: OptimizerConfig = field(default_factory=AdamConfig)
    batch_size: int = 16

    # config related to continued pretraining
    initialize_from_hf: Union[bool, str] = False
    """if provided, this will override the model config in the config. if true, use the default hf checkpoint for this model class"""
    use_hf_model_config: bool = False  # if true, replace the model config with the hf config from the checkpoint

    # TODO: atm we don't support loading from a checkpoint that has a different tokenizer. this is a bit annoying
    # TODO: atm you have to at least specify a levanter model config with the same type as the hf checkpoint

    hf_save_path: Optional[str] = None
    hf_upload: Optional[str] = None
    hf_save_steps: int = 10000
    via_init: bool = False


def main(config: TrainASRConfig):
    tokenizer = config.data.the_tokenizer

    # this is some unpleasant code to allow us to initialize from a hf checkpoint. If this is your first read through,
    # I recommend skipping it for now

    if config.initialize_from_hf:
        if config.trainer.initialize_from is not None:
            raise ValueError("Cannot specify both initialize_from_hf and initialize_from")

        assert isinstance(config.model, HFCompatConfig)
        converter = config.model.default_hf_checkpoint_converter
        if hasattr(tokenizer, "vocab") and tokenizer.vocab != converter.tokenizer.vocab:
            logger.warning("The tokenizers appear to be different. You may want to check this.")

        if isinstance(config.initialize_from_hf, str):
            converter = converter.replaced(
                reference_checkpoint=config.initialize_from_hf,
                tokenizer=tokenizer,
                feature_extractor=config.data.the_feature_extractor,
            )
        else:
            converter = converter.replaced(tokenizer=tokenizer, feature_extractor=config.data.the_feature_extractor)

        if config.use_hf_model_config:
            # TODO: log diff of old and new config
            # NB: gross mutability
            if not config.via_init:
                config.model = converter.config_from_hf_config(converter.default_hf_config)
    elif isinstance(config.model, HFCompatConfig):
        converter = config.model.default_hf_checkpoint_converter
        converter = converter.replaced(tokenizer=tokenizer, feature_extractor=config.data.the_feature_extractor)
    else:
        converter = None

    levanter.initialize(config)
    if config.via_init:
        c = HfConfig.from_pretrained(config.initialize_from_hf)
        config.model = ViaConfig.from_hf_config(c)
        converter = config.model.default_hf_checkpoint_converter
        converter = converter.replaced(tokenizer=tokenizer, feature_extractor=config.data.the_feature_extractor)
    optimizer = config.optimizer.build(config.trainer.num_train_steps)

    # Using the trainer as a context manager does 3 things:
    # 1. Sets the device mesh
    # 2. Sets the axis mapping (for fsdp)
    # 3. Sets the global metrics tracker
    with Trainer(config.trainer, optimizer) as trainer:
        # randomness in jax is tightly controlled by "keys" which are the states of the random number generators
        # this makes deterministic training pretty easy
        seed = config.trainer.seed
        data_key, loader_key, model_key, training_key = jrandom.split(jrandom.PRNGKey(seed), 4)

        # We have two axis_mappings: one for storing the model and optimizer states, and one for compute
        # This allows Zero-3-style parameter sharding, where we shard the parameters and optimizer state across the mesh
        compute_axis_mapping = trainer.compute_axis_mapping
        parameter_axis_mapping = trainer.parameter_axis_mapping

        # some axes we need
        Batch = config.trainer.TrainBatch
        EvalBatch = config.trainer.EvalBatch
        Pos = config.model.Pos  # .resize(112)
        KeyPos = config.model.KeyPos

        eval_datasets = config.data.validation_sets(config.batch_size)
        train_dataset = AudioTextDataset(
            config.data.train_set(config.batch_size),
            Pos,
            config.model.AudioPos,
            KeyPos,
            ignore_index=config.data.pad_token_id,
        )

        # to do partitioning, our dimensions have to be divisible by the size of the physical axes they're mapped to
        # For most things, we just insist you specify the config right, but tokenizers often have strange numbers of
        # tokens: gpt-2 has 50257, for example. So we round up.

        vocab_size = len(tokenizer)
        Vocab = round_axis_for_partitioning(Axis("vocab", vocab_size), parameter_axis_mapping)
        if vocab_size != Vocab.size:
            logger.info(f"Rounding vocab size from {vocab_size} to {Vocab.size} for partitioning")

        if config.initialize_from_hf:
            logger.info(
                "No training checkpoint found. Initializing model from HF checkpoint"
                f" '{converter.reference_checkpoint}'"
            )
            model: ViaASRModel = converter.load_pretrained(
                config.model.asr_model_type,
                ref="WillHeld/via-base",
                axis_mapping=parameter_axis_mapping,
                dtype=trainer.mp.compute_dtype,
            )
            state = trainer.initial_state(
                training_key,
                model=model,
                is_trainable=connector_only(model),
            )
        else:
            logger.info("No checkpoint found. Starting from scratch.")

        if int(state.step) == 0:
            # TODO: I don't love that we init the model twice, but it's not a big deal i think?
            if config.initialize_from_hf:
                # initialize from an hf pretrained model
                logger.info(
                    "No training checkpoint found. Initializing model from HF checkpoint"
                    f" '{converter.reference_checkpoint}'"
                )
                # this is a bit gross, but we want to free up the memory from the model we just built
                state = dataclasses.replace(state, model=None)
                model = converter.load_pretrained(config.model.asr_model_type, axis_mapping=parameter_axis_mapping)
                model = named_jit(trainer.mp.cast_to_param, parameter_axis_mapping)(model)
                state = dataclasses.replace(state, model=model)
            else:
                logger.info("No checkpoint found. Starting from scratch.")

        levanter.tracker.log_summary({"parameter_count": parameter_count(state.model)})

        if len(eval_datasets) == 0:
            logger.warning("No evaluation datasets provided.")

        for name, eval_dataset in eval_datasets.items():
            hax_eval_dataset = AudioTextDataset(
                eval_dataset,
                Pos,
                config.model.AudioPos,
                KeyPos,
                ignore_index=config.data.pad_token_id,
            )
            trainer.add_eval_hook(hax_eval_dataset, name=name)

        trainer.add_hook(callbacks.log_performance_stats(Pos.size, trainer.config.train_batch_size), every=1)
        if config.hf_save_path is not None:
            full_save_path = os.path.join(config.hf_save_path, trainer.run_id)

            trainer.add_hook(
                save_hf_checkpoint_callback(
                    full_save_path, converter, upload_to_hf=config.hf_upload or False, save_feature_extractor=True
                ),
                every=config.hf_save_steps,
            )

        # visualize log probs
        @named_jit(
            in_axis_resources=parameter_axis_mapping,
            axis_resources=compute_axis_mapping,
            out_axis_resources=compute_axis_mapping,
        )
        def compute_log_probs(model, example):
            model = trainer.mp.cast_to_compute(model)
            logprobs = model.compute_loss(example, key=None, reduction=None)
            # roll forward to get the loss for each predicted token
            logprobs = hax.roll(logprobs, 1, Pos)
            return logprobs.rearrange((EvalBatch, Pos)).array

        # data loader. may need to seek to the right place if we're resuming
        train_loader = iter(trainer.sharded_loader(train_dataset, Batch))

        if int(state.step) > 0:
            # step is after the batch, so we need to seek to step
            # TODO: implement iter_data.seek(resume_step +1)
            import tqdm

            for _ in tqdm.tqdm(range(state.step), desc="seeking data for resume"):
                next(train_loader)

        ## OK, actually run training!
        trainer.train(state, train_loader)
        # checkpointer.on_step(last_step, force=True)


if __name__ == "__main__":
    levanter.config.main(main)()
