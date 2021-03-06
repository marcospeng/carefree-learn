import torch

import numpy as np

from typing import *
from cfdata.tabular import DataLoader

from ..base import ModelBase
from ...types import tensor_dict_type
from ...modules.blocks import MLP


@ModelBase.register("fcnn")
class FCNN(ModelBase):
    def __init__(
        self,
        pipeline_config: Dict[str, Any],
        tr_loader: DataLoader,
        cv_loader: DataLoader,
        tr_weights: Optional[np.ndarray],
        cv_weights: Optional[np.ndarray],
        device: torch.device,
        *,
        use_tqdm: bool,
    ):
        super().__init__(
            pipeline_config,
            tr_loader,
            cv_loader,
            tr_weights,
            cv_weights,
            device,
            use_tqdm=use_tqdm,
        )
        self._init_fcnn()

    @property
    def input_sample(self) -> tensor_dict_type:
        return super().input_sample

    def _init_input_config(self) -> None:
        super()._init_input_config()
        if self._fc_in_dim > 512:
            hidden_units = [1024, 1024]
        elif self._fc_in_dim > 256:
            if len(self.tr_data) >= 10000:
                hidden_units = [1024, 1024]
            else:
                hidden_units = [2 * self._fc_in_dim, 2 * self._fc_in_dim]
        else:
            num_tr_data = len(self.tr_data)
            if num_tr_data >= 100000:
                hidden_units = [768, 768]
            elif num_tr_data >= 10000:
                hidden_units = [512, 512]
            else:
                hidden_dim = max(64 if num_tr_data >= 1000 else 32, 2 * self._fc_in_dim)
                hidden_units = [hidden_dim, hidden_dim]
        self.hidden_units = self.config.setdefault("hidden_units", hidden_units)
        self.mapping_configs = self.config.setdefault("mapping_configs", {})

    def _init_fcnn(self) -> None:
        self._init_input_config()
        final_mapping_config = self.config.setdefault("final_mapping_config", {})
        self.mlp = MLP(
            self._fc_in_dim,
            self._fc_out_dim,
            self.hidden_units,
            self.mapping_configs,
            final_mapping_config=final_mapping_config,
        )

    def forward(
        self,
        batch: tensor_dict_type,
        batch_indices: Optional[np.ndarray] = None,
        loader_name: Optional[str] = None,
        **kwargs: Any,
    ) -> tensor_dict_type:
        x_batch = batch["x_batch"]
        net = self._split_features(x_batch, batch_indices, loader_name).merge()
        if self.tr_data.is_ts:
            net = net.view(x_batch.shape[0], -1)
        net = self.mlp(net)
        return {"predictions": net}


__all__ = ["FCNN"]
