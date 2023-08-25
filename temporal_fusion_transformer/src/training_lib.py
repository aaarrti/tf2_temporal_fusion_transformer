from __future__ import annotations

import functools
import os
import platform
from pathlib import Path
from traceback import format_exception
from typing import Literal, Mapping, Protocol, Tuple, TypeVar

import clu.metric_writers
import clu.periodic_actions
import jax
import optax
import orbax.checkpoint.checkpoint_utils
import tensorflow as tf
from absl import logging
from absl_extra import flax_utils, clu_utils
from absl_extra.typing_utils import ParamSpec
from clu.metric_writers import SummaryWriter, AsyncMultiWriter, create_default_writer
from clu.metrics import Average
from etils import epath
from flax.core.frozen_dict import FrozenDict
from flax.struct import dataclass, field
from flax.training.dynamic_scale import DynamicScale
from flax.training.early_stopping import EarlyStopping
from flax.training.train_state import TrainState
from jax import lax
from jax import numpy as jnp
from jax import tree_util
from jaxtyping import Array, Float, PRNGKeyArray, Scalar, jaxtyped
from orbax.checkpoint import CheckpointManagerOptions, CheckpointManager, PyTreeCheckpointHandler, AsyncCheckpointer

from temporal_fusion_transformer.src.config_dict import OptimizerConfig
from temporal_fusion_transformer.src.quantile_loss import QuantileLossFn
from temporal_fusion_transformer.src.tft_layers import InputStruct

P = ParamSpec("P")
R = TypeVar("R")


class EarlyStoppingAdapter:
    should_stop: bool = False

    def __call__(self, *args, training_state: TrainStateContainer, **kwargs):
        if training_state.early_stopping is not None:
            self.should_stop = training_state.early_stopping.should_stop


class ApplyFunc(Protocol):
    def __call__(
        self,
        params: Mapping[str, FrozenDict],
        x: Float[Array, "batch time n"],
        training: bool = False,
        *,
        rngs: Mapping[str, PRNGKeyArray] | None = None,
    ) -> Float[Array, "batch time n"]:
        ...


@jaxtyped
@dataclass
class MetricContainer(clu_utils.AnnotationsCompatibleCollection):
    loss: Average.from_output("loss")


class NoOpWriter:
    def flush(self):
        pass

    def write_scalars(self, *args, **kwargs):
        pass


@jaxtyped
class TrainStateContainer(TrainState):
    apply_fn: ApplyFunc = field(pytree_node=False)
    loss_fn: QuantileLossFn = field(pytree_node=False)
    dropout_key: PRNGKeyArray
    dynamic_scale: DynamicScale | None = None
    early_stopping: EarlyStopping | None = None


def create_writer(logdir: str, collection: str) -> AsyncMultiWriter:
    logdir = epath.Path(logdir)
    logdir /= collection
    writers = [SummaryWriter(os.fspath(logdir))]
    return AsyncMultiWriter(writers)


def make_training_hooks(
    num_training_steps: int,
    epochs: int,
    logdir: str,
    profile: bool = False,
    checkpoint_directory: str = "checkpoints",
    delete_checkpoints_after_training: bool = True,
    report_progress_frequency: int = 50,
    log_metrics_frequency: bool = 100,
    monitor_exception: bool = True,
    save_path: str | None = None,
) -> flax_utils.TrainingHooks:
    logging.info(f"Writing tensorboard logs to {logdir}")

    hooks = flax_utils.TrainingHooks()

    not_running_on_linux = platform.system().lower() != "linux"

    if not_running_on_linux:
        training_writer = NoOpWriter()
    else:
        training_writer = create_writer(logdir, "training")

    training_logger = create_default_writer(None, just_logging=True, collection="training")
    validation_writer = create_default_writer(logdir, just_logging=not_running_on_linux, collection="validation")

    def write_training_metrics_fn(step: int, *args, training_metrics: MetricContainer, **kwargs):
        training_writer.write_scalars(step, training_metrics.compute())

    def log_training_metrics_fn(step: int, *args, training_metrics: MetricContainer, **kwargs):
        training_logger.write_scalars(step, training_metrics.compute())

    def write_validation_metrics_fn(epoch: int, *args, validation_metrics: MetricContainer, **kwargs):
        validation_writer.write_scalars(epoch * num_training_steps, validation_metrics.compute())

    write_training_metrics = clu.periodic_actions.PeriodicCallback(
        every_steps=10,
        callback_fn=write_training_metrics_fn,
        execute_async=True,
    )

    log_training_metrics = clu.periodic_actions.PeriodicCallback(
        every_steps=num_training_steps // log_metrics_frequency,
        callback_fn=log_training_metrics_fn,
    )

    write_validation_metrics = flax_utils.UncheckedPeriodicCallback(
        # I am not sure if the range is inclusive or exclusive
        every_steps=1,
        callback_fn=write_validation_metrics_fn,
        execute_async=True,
    )

    def flush(*args, **kwargs):
        training_writer.flush()
        validation_writer.flush()
        training_logger.flush()

    hooks.on_training_end.append(flush)
    hooks.on_step_end.append(write_training_metrics)
    hooks.on_step_end.append(log_training_metrics)
    hooks.on_epoch_end.append(write_validation_metrics)

    report_progress = clu.periodic_actions.ReportProgress(
        every_steps=num_training_steps // report_progress_frequency,
        num_train_steps=num_training_steps * epochs,
        writer=training_writer,
        every_secs=None,
    )

    def report_progress_func(step: int, *args, **kwargs):
        report_progress(step)

    hooks.on_step_end.append(report_progress_func)
    hooks.on_step_end.append(EarlyStoppingAdapter())

    if profile:
        if not_running_on_linux:
            logging.warning("Profiling is only supported for linux hosts.")
        else:
            profiler = clu.periodic_actions.Profile(
                logdir=logdir, profile_duration_ms=None, every_secs=None, first_profile=5
            )

            def call_profiler(step: int, **kwargs):
                profiler(step)

            hooks.on_step_begin.append(call_profiler)

    add_checkpoint = checkpoint_directory is not None

    if add_checkpoint:
        checkpoint_directory = Path(checkpoint_directory).absolute().as_posix()

        options = CheckpointManagerOptions(
            save_interval_steps=50,
            max_to_keep=5,
            cleanup_tmp_directories=True,
            best_mode="min",
            best_fn=lambda metrics: metrics["loss"],
        )
        mngr = CheckpointManager(
            checkpoint_directory,
            AsyncCheckpointer(PyTreeCheckpointHandler(use_ocdbt=True, write_tree_metadata=True)),
            options,
        )

        def checkpoint_fn(step: int, *, training_metrics: MetricContainer, training_state: TrainStateContainer):
            mngr.save(step, training_state, metrics=training_metrics.as_dict())

        def restore_checkpoint(training_state: TrainStateContainer):
            all_steps = mngr.all_steps(True)
            if len(all_steps) == 0:
                return None

            latest_step = max(all_steps)
            restore_args = orbax.checkpoint.checkpoint_utils.construct_restore_args(training_state)
            restored_dict = mngr.restore(latest_step, restore_kwargs={"restore_args": restore_args})

            restored_optimizer = restore_optimizer_state(training_state.opt_state, restored_dict["opt_state"])
            return training_state.replace(
                dropout_key=restored_dict["dropout_key"],
                params=restored_dict["params"],
                step=restored_dict["step"],
                dynamic_scale=restored_dict["dynamic_scale"],
                opt_state=restored_optimizer,
            )

        hooks.on_training_begin.append(restore_checkpoint)
        hooks.on_step_end.append(checkpoint_fn)

        if delete_checkpoints_after_training:

            def delete_checkpoints(*args, **kwargs):
                for step in mngr.all_steps():
                    mngr.delete(step)

            hooks.on_training_end.append(delete_checkpoints)

        if save_path is not None:

            def save_weight_fn(*args, training_state: TrainStateContainer, **kwargs):
                flax_utils.save_as_msgpack(training_state.params, save_path)

            hooks.on_training_end.append(save_weight_fn)

    if monitor_exception:

        def persist_nan_causing_args(
            state: TrainStateContainer,
            x_batch: InputStruct,
            y_batch: Float[Array, "batch time n"],
            step_type: flax_utils.StepType,
            exception: Exception,
        ):
            if isinstance(exception, FloatingPointError):
                ex_str = format_exception(exception)
                logging.error(
                    f"Step number {int(state.step)} failed with {format_exception(exception)} for {x_batch = }, {y_batch = }"
                )
                data = {
                    "state": state,
                    "x_batch": x_batch,
                    "y_batch": y_batch,
                    "exception": ex_str,
                    "step_type": step_type,
                }
                flax_utils.save_as_msgpack(data, "fp_error.msgpack")
            raise

        hooks.on_error.append(persist_nan_causing_args)

    return hooks


@jaxtyped
@jax.jit
def single_device_train_step(
    state: TrainStateContainer,
    x_batch: InputStruct,
    y_batch: Float[Array, "batch time n"],
) -> Tuple[TrainStateContainer, MetricContainer]:
    dropout_train_key = jax.random.fold_in(key=state.dropout_key, data=state.step)

    def loss_fn(params: FrozenDict) -> Float[Scalar]:
        # pass training=True as positional args, since flax.nn.jit does not support kwargs.
        y = state.apply_fn({"params": params}, x_batch, True, rngs={"dropout": dropout_train_key})
        y_loss = state.loss_fn(y_batch, y).mean()
        return y_loss

    if state.dynamic_scale is not None:
        # loss scaling logic is taken from https://github.com/google/flax/blob/main/examples/wmt/train.py#L177
        dynamic_scale, is_fin, loss, grads = state.dynamic_scale.value_and_grad(loss_fn)(state.params)
        state.replace(dynamic_scale=dynamic_scale)
        state = state.apply_gradients(grads=grads)
    else:
        dynamic_scale, is_fin = None, None
        loss, grads = jax.value_and_grad(loss_fn)(state.params)

    state = state.apply_gradients(grads=grads)
    if state.dynamic_scale is not None:
        select_fn = tree_util.Partial(jnp.where, is_fin)
        state = state.replace(
            opt_state=jax.tree_util.tree_map(select_fn, state.opt_state, state.opt_state),
            params=jax.tree_util.tree_map(select_fn, state.params, state.params),
        )

    if state.early_stopping is not None:
        state = state.replace(early_stopping=state.early_stopping.update(loss)[1])

    metrics = MetricContainer.single_from_model_output(loss=loss)
    return state, metrics


@jaxtyped
@jax.jit
def single_device_validation_step(
    state: TrainStateContainer,
    x_batch: InputStruct,
    y_batch: Float[Array, "batch time n"],
) -> MetricContainer:
    y = state.apply_fn({"params": state.params}, x_batch)
    loss = state.loss_fn(y_batch, y).mean()
    metrics = MetricContainer.single_from_model_output(loss=loss)
    return metrics


@jaxtyped
@functools.partial(jax.pmap, axis_name="i")
def multi_device_train_step(
    state: TrainStateContainer,
    x_batch: InputStruct,
    y_batch: Float[Array, "batch time n"],
) -> Tuple[TrainStateContainer, MetricContainer]:
    dropout_train_key = jax.random.fold_in(key=state.dropout_key, data=state.step)

    def loss_fn(params: FrozenDict) -> float:
        y = state.apply_fn({"params": params}, x_batch, True, rngs={"dropout": dropout_train_key})
        y_loss = state.loss_fn(y_batch, y).mean()
        return y_loss

    if state.dynamic_scale is not None:
        dynamic_scale, is_fin, loss, grads = state.dynamic_scale.value_and_grad(loss_fn, axis_name="i")(state.params)
        state.replace(dynamic_scale=dynamic_scale)
    else:
        dynamic_scale, is_fin = None, None
        loss, grads = jax.value_and_grad(loss_fn)(state.params)
        grads = lax.pmean(grads, axis_name="i")

    state = state.apply_gradients(grads=grads)
    if state.dynamic_scale is not None:
        select_fn = tree_util.Partial(jnp.where, is_fin)
        state = state.replace(
            opt_state=jax.tree_util.tree_map(select_fn, state.opt_state, state.opt_state),
            params=jax.tree_util.tree_map(select_fn, state.params, state.params),
        )

    if state.early_stopping is not None:
        loss = lax.pmean(loss, axis_name="i")
        state = state.replace(early_stopping=state.early_stopping.update(loss)[1])

    metrics = MetricContainer.gather_from_model_output(loss=loss, axis_name="i")
    return state, metrics


@jaxtyped
@functools.partial(jax.pmap, axis_name="i")
def multi_device_validation_step(
    state: TrainStateContainer,
    x_batch: InputStruct,
    y_batch: Float[Array, "batch time n"],
) -> MetricContainer:
    y = state.apply_fn({"params": state.params}, x_batch)
    loss = state.loss_fn(y_batch, y)
    loss = lax.pmean(loss, axis_name="i")
    metrics = MetricContainer.gather_from_model_output(loss=loss, axis_name="i")
    return metrics


def load_dataset(
    data_dir: str,
    batch_size: int,
    prng_seed: int,
    shuffle_buffer_size: int = 2048,
    dtype=jnp.float32,
    full_reshuffle: bool = False,
) -> Tuple[tf.data.Dataset, tf.data.Dataset]:
    """

    Parameters
    ----------
    data_dir
    batch_size
    shuffle_buffer_size:
        If set to None, will do a full-reshuffle.
    prng_seed
    dtype
    full_reshuffle:
        If set to true, will reshuffle complete dataset once before training.
        Warning, this will need to load complete dataset into memory.

    Returns
    -------

    """

    tf_dtype = tf.dtypes.as_dtype(dtype)

    def downcast_input(x, y):
        return tf.cast(x, tf_dtype), tf.cast(y, tf_dtype)

    def load_fn(split: Literal["training", "validation"]) -> tf.data.Dataset:
        ds = tf.data.Dataset.load(f"{data_dir}/{split}", compression="GZIP")

        if full_reshuffle:
            ds = ds.shuffle(int(ds.cardinality()), seed=prng_seed, reshuffle_each_iteration=False)

        return (
            ds.batch(batch_size, drop_remainder=True, num_parallel_calls=tf.data.AUTOTUNE)
            .shuffle(shuffle_buffer_size, seed=prng_seed, reshuffle_each_iteration=True)
            .map(downcast_input)
            .cache()
            .prefetch(tf.data.AUTOTUNE)
        )

    training_ds = load_fn("training")
    validation_ds = load_fn("validation")
    return training_ds, validation_ds


def make_optimizer(config: OptimizerConfig, num_training_steps: int, epochs: int) -> optax.GradientTransformation:
    learning_rate = config.learning_rate
    if config.decay_steps != 0:
        decay_steps = num_training_steps * epochs * config.decay_steps
        learning_rate = optax.cosine_decay_schedule(learning_rate, decay_steps, config.decay_alpha)
    tx = optax.adam(learning_rate)
    if config.clipnorm != 0:
        tx = optax.chain(optax.adaptive_grad_clip(config.clipnorm), tx)

    if config.ema != 0:
        tx = optax.chain(tx, optax.ema(config.ema))

    return tx


def restore_optimizer_state(opt_state, restored):
    return tree_util.tree_unflatten(tree_util.tree_flatten(opt_state)[1], tree_util.tree_leaves(restored))
