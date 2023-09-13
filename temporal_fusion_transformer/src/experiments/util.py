from __future__ import annotations

from pathlib import Path
from typing import List, Mapping, TypedDict

import numpy as np
import polars as pl
import tensorflow as tf
from absl_extra.flax_utils import save_as_msgpack
from flax.serialization import msgpack_restore
from jax.tree_util import tree_map
from sklearn.preprocessing import LabelEncoder, StandardScaler
from toolz import functoolz
from tqdm.auto import tqdm


class StandardScalerPytree(TypedDict):
    var: np.ndarray
    mean: np.ndarray
    scale: np.ndarray


class LabelEncoderPytree(TypedDict):
    classes: Mapping[str, str]


def standard_scaler_to_pytree(sc: StandardScaler) -> StandardScalerPytree:
    return {"var": sc.var_, "mean": sc.mean_, "scale": sc.scale_}


def pytree_to_standard_scaler(pytree: StandardScalerPytree) -> StandardScaler:
    sc = StandardScaler()
    sc.var_ = pytree["var"]
    sc.mean_ = pytree["mean"]
    sc.scale_ = pytree["scale"]
    return sc


def label_encoder_to_pytree(le: LabelEncoder) -> LabelEncoderPytree:
    classes = le.classes_
    if isinstance(classes, np.ndarray) and isinstance(classes[0], str):
        classes = classes.tolist()

    return {"classes": classes}


def is_standard_scaler_pytree(pytree) -> bool:
    return (
        isinstance(pytree, Mapping) and "var" in pytree and "mean" in pytree and "scale" in pytree and len(pytree) == 3
    )


def is_label_encoder_pytree(pytree) -> bool:
    return isinstance(pytree, Mapping) and "classes" in pytree and len(pytree) == 1


def pytree_to_label_encoder(pytree: LabelEncoderPytree) -> LabelEncoder:
    le = LabelEncoder()
    classes = pytree["classes"]
    if isinstance(classes, Mapping):
        classes = np.asarray(list(classes.values()))
    le.classes_ = classes
    return le


def serialize_preprocessor(
    preprocessor: Mapping[str, ...],
    filename: str | Path,
):
    if isinstance(filename, str):
        filename = Path(filename)

    if filename.is_dir():
        filename = filename.joinpath("preprocessor.msgpack")

    filename = filename.as_posix()

    def is_leaf(sc):
        return isinstance(sc, (StandardScaler, LabelEncoder))

    def map_fn(x):
        if isinstance(x, StandardScaler):
            return standard_scaler_to_pytree(x)
        else:
            return label_encoder_to_pytree(x)

    pytree = tree_map(map_fn, preprocessor, is_leaf=is_leaf)
    save_as_msgpack(pytree, filename)


def deserialize_preprocessor(filename: str | Path) -> Mapping[str, ...]:
    if isinstance(filename, Path):
        filename = filename.as_posix()

    with open(filename, "rb") as file:
        byte_date = file.read()

    restored = msgpack_restore(byte_date)

    def map_fn(x):
        if is_standard_scaler_pytree(x):
            return pytree_to_standard_scaler(x)
        else:
            return pytree_to_label_encoder(x)

    def is_leaf(x):
        return is_standard_scaler_pytree(x) or is_label_encoder_pytree(x)

    preprocessor = tree_map(map_fn, restored, is_leaf=is_leaf)
    return preprocessor


def time_series_from_array(
    df: pl.DataFrame, inputs: List[str], targets: List[str], total_time_steps: int, id_column: str
) -> tf.data.Dataset:
    from keras.utils import timeseries_dataset_from_array

    # for some reason, keras would generate targets of shape [1, n] and inputs [time_steps, n],
    # but we need time-steps for y_batch also, we need is [time_steps, m]. We don't need `sequence_stride`,
    # since we don't want any synthetic repetitions.
    # -1 for TARGETS
    num_inputs = len(inputs)

    time_series_list = []
    groups = list(df.groupby(id_column))

    def make_time_series_fn(sub_df: pl.DataFrame) -> tf.data.Dataset:
        x: np.ndarray = sub_df[inputs + targets].to_numpy(order="c")

        time_series: tf.data.Dataset = timeseries_dataset_from_array(
            x,
            None,
            total_time_steps,
            batch_size=None,
        )
        time_series = time_series.map(
            lambda x: (x[..., :num_inputs], x[..., num_inputs:]),
            num_parallel_calls=tf.data.AUTOTUNE,
            deterministic=False,
        )
        return time_series

    if len(groups) == 0:
        return make_time_series_fn(df)

    for id_i, df_i in tqdm(groups, desc="Converting to time-series dataset"):
        time_series_i = make_time_series_fn(df_i)
        time_series_list.append(time_series_i)

    return functoolz.reduce(lambda a, b: a.concatenate(b), time_series_list)
