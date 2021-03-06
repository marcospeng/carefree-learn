import torch

from typing import *
from cftool.ml import Anneal
from cftool.misc import LoggingMixin
from torch.nn.functional import l1_loss, relu, softplus


from ...losses import LossBase
from ...types import tensor_dict_type
from ...modules.auxiliary import MTL


class DDRLoss(LossBase, LoggingMixin):
    def _init_config(self, config: Dict[str, Any]) -> None:
        device = config["device"]
        self._joint_training = config["joint_training"]
        self._use_dynamic_dual_loss_weights = config["use_dynamic_weights"]
        self._use_anneal, self._anneal_step = (
            config["use_anneal"],
            config["anneal_step"],
        )
        self._median_pressure = config.setdefault("median_pressure", 3.0)
        self._median_pressure_inv = 1.0 / self._median_pressure
        self.mtl = MTL(16, config["mtl_method"])
        self._target_loss_warned = False
        self._zero = torch.zeros([1, 1], dtype=torch.float32).to(device)
        if self._use_anneal:
            self._median_anneal: Anneal
            self._main_anneal: Anneal
            self._monotonous_anneal: Anneal
            self._anchor_anneal: Anneal
            self._dual_anneal: Anneal
            self._recover_anneal: Anneal
            self._pressure_anneal: Anneal
            anneal_config = config.setdefault("anneal_config", {})
            anneal_methods = anneal_config.setdefault("methods", {})
            anneal_ratios = anneal_config.setdefault("ratios", {})
            anneal_floors = anneal_config.setdefault("floors", {})
            anneal_ceilings = anneal_config.setdefault("ceilings", {})
            default_anneal_methods = {
                "median_anneal": "linear",
                "main_anneal": "linear",
                "monotonous_anneal": "sigmoid",
                "anchor_anneal": "linear",
                "dual_anneal": "sigmoid",
                "recover_anneal": "sigmoid",
                "pressure_anneal": "sigmoid",
            }
            default_anneal_ratios = {
                "median_anneal": 0.25,
                "main_anneal": 0.25,
                "monotonous_anneal": 0.2,
                "anchor_anneal": 0.2,
                "dual_anneal": 0.75,
                "recover_anneal": 0.75,
                "pressure_anneal": 0.5,
            }
            default_anneal_floors = {
                "median_anneal": 1.0,
                "main_anneal": 0.0,
                "monotonous_anneal": 0.0,
                "anchor_anneal": 0.0,
                "dual_anneal": 0.0,
                "recover_anneal": 0.0,
                "pressure_anneal": 0.0,
            }
            default_anneal_ceilings = {
                "median_anneal": 2.5,
                "main_anneal": 0.8,
                "monotonous_anneal": 2.5,
                "anchor_anneal": 2.0,
                "dual_anneal": 0.1,
                "recover_anneal": 0.1,
                "pressure_anneal": 1.0,
            }
            for anneal in default_anneal_methods:
                anneal_methods.setdefault(anneal, default_anneal_methods[anneal])
                anneal_ratios.setdefault(anneal, default_anneal_ratios[anneal])
                anneal_floors.setdefault(anneal, default_anneal_floors[anneal])
                anneal_ceilings.setdefault(anneal, default_anneal_ceilings[anneal])
            for anneal in default_anneal_methods:
                attr = f"_{anneal}"
                if anneal_methods[anneal] is None:
                    setattr(self, attr, None)
                else:
                    setattr(
                        self,
                        attr,
                        Anneal(
                            anneal_methods[anneal],
                            round(self._anneal_step * anneal_ratios[anneal]),
                            anneal_floors[anneal],
                            anneal_ceilings[anneal],
                        ),
                    )

    @staticmethod
    def _pdf_loss(pdf: torch.Tensor) -> torch.Tensor:
        negative_mask = pdf <= 1e-8
        monotonous_loss = torch.sum(-pdf[negative_mask])
        log_likelihood_loss = torch.sum(-torch.log(pdf[~negative_mask]))
        return (monotonous_loss + log_likelihood_loss) / len(pdf)

    def _core(  # type: ignore
        self,
        predictions: tensor_dict_type,
        target: torch.Tensor,
        *,
        check_monotonous_only: bool = False,
    ) -> Tuple[torch.Tensor, tensor_dict_type]:
        # anneal
        if not self._use_anneal or not self.training or check_monotonous_only:
            main_anneal = median_anneal = None
            monotonous_anneal = anchor_anneal = None
            dual_anneal = recover_anneal = pressure_anneal = None
        else:
            main_anneal = None if self._main_anneal is None else self._main_anneal.pop()
            median_anneal = (
                None if self._median_anneal is None else self._median_anneal.pop()
            )
            monotonous_anneal = (
                None
                if self._monotonous_anneal is None
                else self._monotonous_anneal.pop()
            )
            anchor_anneal = (
                None if self._median_anneal is None else self._anchor_anneal.pop()
            )
            dual_anneal = (
                None if self._median_anneal is None else self._dual_anneal.pop()
            )
            recover_anneal = (
                None if self._median_anneal is None else self._recover_anneal.pop()
            )
            pressure_anneal = (
                None if self._pressure_anneal is None else self._pressure_anneal.pop()
            )
            self._last_main_anneal, self._last_pressure_anneal = (
                main_anneal,
                pressure_anneal,
            )
        if self._use_anneal and check_monotonous_only:
            main_anneal, pressure_anneal = (
                self._last_main_anneal,
                self._last_pressure_anneal,
            )
        # median
        median = predictions["predictions"]
        median_losses = l1_loss(median, target, reduction="none")
        if median_anneal is not None:
            median_losses = median_losses * median_anneal
        # get
        anchor_batch, cdf_raw = map(predictions.get, ["anchor_batch", "cdf_raw"])
        sampled_anchors, sampled_cdf_raw = map(
            predictions.get, ["sampled_anchors", "sampled_cdf_raw"]
        )
        quantile_batch, median_residual, quantile_residual, quantile_sign = map(
            predictions.get,
            ["quantile_batch", "median_residual", "quantile_residual", "quantile_sign"],
        )
        sampled_quantiles, sampled_quantile_residual = map(
            predictions.get, ["sampled_quantiles", "sampled_quantile_residual"]
        )
        pdf, sampled_pdf = map(predictions.get, ["pdf", "sampled_pdf"])
        qr_gradient, sampled_qr_gradient = map(
            predictions.get, ["quantile_residual_gradient", "sampled_qr_gradient"]
        )
        dual_quantile, quantile_cdf_raw = map(
            predictions.get, ["dual_quantile", "quantile_cdf_raw"]
        )
        dual_cdf, cdf_quantile_residual = map(
            predictions.get, ["dual_cdf", "cdf_quantile_residual"]
        )
        # cdf
        fetch_cdf = cdf_raw is not None
        cdf_anchor_losses = pdf_losses = None
        if not fetch_cdf or check_monotonous_only:
            cdf_losses = None
        else:
            assert cdf_raw is not None
            assert anchor_batch is not None
            cdf_losses = self._get_cdf_losses(target, cdf_raw, anchor_batch)
            if main_anneal is not None:
                cdf_losses = cdf_losses * main_anneal
            if sampled_cdf_raw is not None:
                assert sampled_anchors is not None
                cdf_anchor_losses = self._get_cdf_losses(
                    target,
                    sampled_cdf_raw,
                    sampled_anchors,
                )
                if anchor_anneal is not None:
                    cdf_anchor_losses = cdf_anchor_losses * anchor_anneal
        # pdf losses
        if pdf is not None and sampled_pdf is not None:
            pdf_losses = self._pdf_loss(pdf) + self._pdf_loss(sampled_pdf)
            if anchor_anneal is not None:
                pdf_losses = pdf_losses * monotonous_anneal
        # quantile
        fetch_quantile = quantile_residual is not None
        quantile_anchor_losses = None
        quantile_monotonous_losses: Optional[torch.Tensor] = None
        if not fetch_quantile or check_monotonous_only:
            median_residual_losses = quantile_losses = None
        else:
            assert quantile_sign is not None
            assert quantile_batch is not None
            assert median_residual is not None
            assert quantile_residual is not None
            target_median_residual = target - predictions["median_detach"]
            median_residual_losses = self._get_median_residual_losses(
                target_median_residual,
                median_residual,
                quantile_sign,
            )
            if anchor_anneal is not None:
                median_residual_losses = median_residual_losses * anchor_anneal
            quantile_losses = self._get_quantile_residual_losses(
                target_median_residual,
                quantile_residual,
                quantile_batch,
            )
            quantile_losses = quantile_losses + median_residual_losses
            if main_anneal is not None:
                quantile_losses = quantile_losses * main_anneal
            if sampled_quantile_residual is not None:
                assert sampled_quantiles is not None
                assert sampled_quantile_residual is not None
                quantile_anchor_losses = self._get_quantile_residual_losses(
                    target_median_residual,
                    sampled_quantile_residual,
                    sampled_quantiles,
                )
                if anchor_anneal is not None:
                    quantile_anchor_losses = quantile_anchor_losses * anchor_anneal
        # median pressure
        if not fetch_quantile:
            median_pressure_losses = None
        else:
            median_pressure_losses = self._get_median_pressure_losses(predictions)
            if pressure_anneal is not None:
                median_pressure_losses = median_pressure_losses * pressure_anneal
        # quantile monotonous
        qm_losses_list: List[torch.Tensor] = []
        if qr_gradient is not None and sampled_qr_gradient is not None:
            qr_g_losses = [relu(-qr_gradient), relu(-sampled_qr_gradient)]
            qm_losses_list += qr_g_losses
        if median_residual is not None and quantile_sign is not None:
            qm_losses_list.append(
                self._get_median_residual_monotonous_losses(
                    median_residual, quantile_sign
                )
            )
        if qm_losses_list:
            qm_losses: torch.Tensor = sum(qm_losses_list)  # type: ignore
            if anchor_anneal is not None:
                assert monotonous_anneal is not None
                qm_losses = qm_losses * monotonous_anneal
            quantile_monotonous_losses = qm_losses
        # dual
        if (
            not self._joint_training
            or not fetch_cdf
            or not fetch_quantile
            or check_monotonous_only
        ):
            dual_cdf_losses = dual_quantile_losses = None
            cdf_recover_losses = quantile_recover_losses = None
        else:
            assert cdf_losses is not None
            assert anchor_batch is not None
            assert dual_quantile is not None
            assert dual_cdf is not None
            assert quantile_batch is not None
            assert quantile_losses is not None
            # dual cdf (cdf -> quantile [recover loss] -> cdf [dual loss])
            (
                quantile_recover_losses,
                quantile_recover_losses_weights,
            ) = self._get_dual_recover_losses(dual_quantile, anchor_batch, cdf_losses)
            if quantile_cdf_raw is None:
                dual_quantile_losses = None
            else:
                assert anchor_batch is not None
                dual_quantile_losses = self._get_cdf_losses(
                    target,
                    quantile_cdf_raw,
                    anchor_batch,
                )
                if (
                    quantile_recover_losses is None
                    or not self._use_dynamic_dual_loss_weights
                ):
                    dual_quantile_losses_weights = 1.0
                else:
                    quantile_recover_losses_detach = quantile_recover_losses.detach()
                    dual_quantile_losses_weights = 0.5 * (
                        quantile_recover_losses_weights
                        + 1 / (1 + 2 * torch.tanh(quantile_recover_losses_detach))
                    )
                dual_quantile_losses = (
                    dual_quantile_losses * dual_quantile_losses_weights
                )
            if quantile_recover_losses is not None:
                quantile_recover_losses = (
                    quantile_recover_losses * quantile_recover_losses_weights
                )
            # dual quantile (quantile -> cdf [recover loss] -> quantile [dual loss])
            (
                cdf_recover_losses,
                cdf_recover_losses_weights,
            ) = self._get_dual_recover_losses(dual_cdf, quantile_batch, quantile_losses)
            if cdf_quantile_residual is None:
                dual_cdf_losses = None
            else:
                dual_cdf_losses = self._get_quantile_residual_losses(
                    target, cdf_quantile_residual, quantile_batch
                )
                if (
                    cdf_recover_losses is None
                    or not self._use_dynamic_dual_loss_weights
                ):
                    dual_cdf_losses_weights = 1.0
                else:
                    cdf_recover_losses_detach = cdf_recover_losses.detach()
                    dual_cdf_losses_weights = 0.5 * (
                        cdf_recover_losses_weights
                        + 1 / (1 + 10 * cdf_recover_losses_detach)
                    )
                dual_cdf_losses = (
                    dual_cdf_losses * dual_cdf_losses_weights
                ) + median_residual_losses
            if cdf_recover_losses is not None:
                cdf_recover_losses = cdf_recover_losses * cdf_recover_losses_weights
        if dual_anneal is not None:
            if dual_cdf_losses is not None:
                dual_cdf_losses = dual_cdf_losses * dual_anneal
            if dual_quantile_losses is not None:
                dual_quantile_losses = dual_quantile_losses * dual_anneal
        if recover_anneal is not None:
            if cdf_recover_losses is not None:
                cdf_recover_losses = cdf_recover_losses * recover_anneal
            if quantile_recover_losses is not None:
                quantile_recover_losses = quantile_recover_losses * recover_anneal
        # combine
        if check_monotonous_only:
            losses = {}
        else:
            losses = {"median": median_losses}
            if not self._joint_training:
                if cdf_anchor_losses is not None:
                    losses["cdf_anchor"] = cdf_anchor_losses
                if quantile_anchor_losses is not None:
                    losses["quantile_anchor"] = quantile_anchor_losses
            else:
                if fetch_cdf:
                    assert cdf_losses is not None
                    losses["cdf"] = cdf_losses
                    if cdf_anchor_losses is not None:
                        losses["cdf_anchor"] = cdf_anchor_losses
                if fetch_quantile:
                    assert quantile_losses is not None
                    losses["quantile"] = quantile_losses
                    if quantile_anchor_losses is not None:
                        losses["quantile_anchor"] = quantile_anchor_losses
                if fetch_cdf and fetch_quantile:
                    assert quantile_recover_losses is not None
                    assert cdf_recover_losses is not None
                    assert dual_quantile_losses is not None
                    assert dual_cdf_losses is not None
                    losses["quantile_recover"] = quantile_recover_losses
                    losses["cdf_recover"] = cdf_recover_losses
                    losses["dual_quantile"] = dual_quantile_losses
                    losses["dual_cdf"] = dual_cdf_losses
        if median_residual_losses is not None:
            losses["median_residual_losses"] = median_residual_losses
        if median_pressure_losses is not None:
            key = (
                "synthetic_median_pressure_losses"
                if check_monotonous_only
                else "median_pressure_losses"
            )
            losses[key] = median_pressure_losses
        if pdf_losses is not None:
            key = "synthetic_pdf" if check_monotonous_only else "pdf"
            losses[key] = pdf_losses
        if quantile_monotonous_losses is not None:
            key = (
                "synthetic_quantile_monotonous"
                if check_monotonous_only
                else "quantile_monotonous"
            )
            losses[key] = quantile_monotonous_losses
        if not losses:
            zero = self._zero.repeat(len(target), 1)
            return zero, {"loss": zero}
        if not self.mtl.registered:
            self.mtl.register(list(losses.keys()))
        return self.mtl(losses), losses

    def forward(  # type: ignore
        self,
        predictions: tensor_dict_type,
        target: torch.Tensor,
        *,
        check_monotonous_only: bool = False,
    ) -> Tuple[torch.Tensor, tensor_dict_type]:
        losses, losses_dict = self._core(
            predictions,
            target,
            check_monotonous_only=check_monotonous_only,
        )
        reduced_losses = self._reduce(losses)
        reduced_losses_dict = {k: self._reduce(v) for k, v in losses_dict.items()}
        return reduced_losses, reduced_losses_dict

    def _get_dual_recover_losses(
        self,
        dual_prediction: torch.Tensor,
        another_input_batch: torch.Tensor,
        another_losses: torch.Tensor,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor]]:
        if dual_prediction is None:
            recover_losses = recover_loss_weights = None
        else:
            recover_losses = torch.abs(another_input_batch - dual_prediction)
            if not self._use_dynamic_dual_loss_weights:
                device = recover_losses.device
                recover_loss_weights = torch.tensor([1.0], device=device)
            else:
                another_losses_detach = another_losses.detach()
                recover_loss_weights = 1 / (1 + 2 * torch.tanh(another_losses_detach))
        return recover_losses, recover_loss_weights

    @staticmethod
    def _get_cdf_losses(
        target: torch.Tensor,
        cdf_raw: torch.Tensor,
        anchor_batch: torch.Tensor,
    ) -> torch.Tensor:
        indicative = (target <= anchor_batch).to(torch.float32)
        return -indicative * cdf_raw + softplus(cdf_raw)

    @staticmethod
    def _get_median_residual_monotonous_losses(
        median_residual: torch.Tensor,
        quantile_sign: torch.Tensor,
    ) -> torch.Tensor:
        return relu(-median_residual * quantile_sign)

    @staticmethod
    def _get_quantile_residual_losses(
        target_residual: torch.Tensor,
        quantile_residual: torch.Tensor,
        quantile_batch: torch.Tensor,
    ) -> torch.Tensor:
        quantile_error = target_residual - quantile_residual
        q1 = quantile_batch * quantile_error
        q2 = (quantile_batch - 1) * quantile_error
        return torch.max(q1, q2)

    def _get_median_residual_losses(
        self,
        target_median_residual: torch.Tensor,
        median_residual: torch.Tensor,
        quantile_sign: torch.Tensor,
    ) -> torch.Tensor:
        same_sign_mask = quantile_sign * torch.sign(target_median_residual) > 0
        tmr, mr = map(
            lambda tensor: tensor[same_sign_mask],
            [target_median_residual, median_residual],
        )
        median_residual_loss = self._median_pressure * torch.abs(tmr - mr).mean()
        residual_monotonous_losses = DDRLoss._get_median_residual_monotonous_losses(
            median_residual, quantile_sign
        )
        return median_residual_loss + residual_monotonous_losses

    def _get_median_pressure_losses(
        self,
        predictions: tensor_dict_type,
    ) -> torch.Tensor:
        pp_dict: tensor_dict_type = predictions["pp_dict"]  # type: ignore
        pn_dict: tensor_dict_type = predictions["pn_dict"]  # type: ignore
        additive_pos, additive_neg = pp_dict["add"], pn_dict["add"]
        multiply_pos, multiply_neg = pp_dict["mul"], pn_dict["mul"]
        # additive net & multiply net are tend to be zero here
        # because median pressure batch receives 0.5 as input
        loss: torch.Tensor = sum(  # type: ignore
            torch.max(
                -self._median_pressure * sub_quantile,
                self._median_pressure_inv * sub_quantile,
            )
            for sub_quantile in [
                additive_pos,
                -additive_neg,
                multiply_pos,
                multiply_neg,
            ]
        )
        return loss


__all__ = ["DDRLoss"]
