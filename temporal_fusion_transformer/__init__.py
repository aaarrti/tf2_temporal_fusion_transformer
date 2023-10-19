from typing import TYPE_CHECKING
import os

#if TYPE_CHECKING:
#    from temporal_fusion_transformer.src.config_dict import ConfigDict
#    from temporal_fusion_transformer.src.modeling.tft_model import TftOutputs
#
#from temporal_fusion_transformer.src import experiments, inference
#from temporal_fusion_transformer.src.modeling.tft_model import TemporalFusionTransformer
#from temporal_fusion_transformer.src.training import training
#from temporal_fusion_transformer.src.training.training_hooks import (
#    EarlyStoppingConfig,
#    HooksConfig,
#)

import jax

jax.config.update("jax_softmax_custom_jvp", True)
