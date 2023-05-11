import os
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Type, Union

import cloudpickle
import numpy as np
import pandas as pd
from typing_extensions import Unpack

from snowflake.ml._internal import type_utils
from snowflake.ml.model import (
    _model_meta as model_meta_api,
    custom_model,
    model_signature,
    type_hints as model_types,
)
from snowflake.ml.model._handlers import _base

if TYPE_CHECKING:
    import sklearn.base
    import sklearn.pipeline


class _SKLModelHandler(_base._ModelHandler):
    """Handler for scikit-learn based model.

    Currently sklearn.base.BaseEstimator and sklearn.pipeline.Pipeline based classes are supported.
    """

    handler_type = "sklearn"
    DEFAULT_TARGET_METHODS = ["predict", "transform", "predict_proba", "predict_log_proba", "decision_function"]

    @staticmethod
    def can_handle(model: model_types.ModelType) -> bool:
        return (
            type_utils.LazyType("sklearn.base.BaseEstimator").isinstance(model)
            or type_utils.LazyType("sklearn.pipeline.Pipeline").isinstance(model)
        ) and any(
            (hasattr(model, method) and callable(getattr(model, method, None)))
            for method in _SKLModelHandler.DEFAULT_TARGET_METHODS
        )

    @staticmethod
    def _save_model(
        name: str,
        model: Union["sklearn.base.BaseEstimator", "sklearn.pipeline.Pipeline"],
        model_meta: model_meta_api.ModelMetadata,
        model_blobs_dir_path: str,
        sample_input: Optional[model_types.SupportedDataType] = None,
        **kwargs: Unpack[model_types.SKLModelSaveOptions],
    ) -> None:
        import sklearn.base
        import sklearn.pipeline

        assert isinstance(model, sklearn.base.BaseEstimator) or isinstance(model, sklearn.pipeline.Pipeline)

        if model_meta._signatures is None:
            # In this case sample_input should be available, because of the check in save_model.
            assert sample_input is not None
            target_methods = kwargs.pop("target_methods", None)
            if target_methods is None:
                target_methods = [
                    method
                    for method in _SKLModelHandler.DEFAULT_TARGET_METHODS
                    if hasattr(model, method) and callable(getattr(model, method, None))
                ]
            else:
                for method_name in target_methods:
                    if not callable(getattr(model, method_name, None)):
                        raise ValueError(f"Target method {method_name} is not callable.")
                    if method_name not in _SKLModelHandler.DEFAULT_TARGET_METHODS:
                        raise ValueError(f"Target method {method_name} is not supported.")

            model_meta._signatures = {}
            for method_name in target_methods:
                target_method = getattr(model, method_name)
                sig = model_signature.infer_signature(sample_input, target_method(sample_input))
                model_meta._signatures[method_name] = sig
        else:
            for method_name in model_meta._signatures.keys():
                if not callable(getattr(model, method_name, None)):
                    raise ValueError(f"Target method {method_name} is not callable.")
                if method_name not in _SKLModelHandler.DEFAULT_TARGET_METHODS:
                    raise ValueError(f"Target method {method_name} is not supported.")

        model_blob_path = os.path.join(model_blobs_dir_path, name)
        os.makedirs(model_blob_path, exist_ok=True)
        with open(os.path.join(model_blob_path, _SKLModelHandler.MODEL_BLOB_FILE), "wb") as f:
            cloudpickle.dump(model, f)
        base_meta = model_meta_api._ModelBlobMetadata(
            name=name, model_type=_SKLModelHandler.handler_type, path=_SKLModelHandler.MODEL_BLOB_FILE
        )
        model_meta.models[name] = base_meta
        model_meta._include_if_absent([("scikit-learn", "scikit-learn")])

    @staticmethod
    def _load_model(
        name: str, model_meta: model_meta_api.ModelMetadata, model_blobs_dir_path: str
    ) -> Union["sklearn.base.BaseEstimator", "sklearn.pipeline.Pipeline"]:
        model_blob_path = os.path.join(model_blobs_dir_path, name)
        if not hasattr(model_meta, "models"):
            raise ValueError("Ill model metadata found.")
        model_blobs_metadata = model_meta.models
        if name not in model_blobs_metadata:
            raise ValueError(f"Blob of model {name} does not exist.")
        model_blob_metadata = model_blobs_metadata[name]
        model_blob_filename = model_blob_metadata.path
        with open(os.path.join(model_blob_path, model_blob_filename), "rb") as f:
            m = cloudpickle.load(f)
        return m

    @staticmethod
    def _load_as_custom_model(
        name: str, model_meta: model_meta_api.ModelMetadata, model_blobs_dir_path: str
    ) -> custom_model.CustomModel:
        """Create a custom model class wrap for unified interface when being deployed. The predict method will be
        re-targeted based on target_method metadata.

        Args:
            name: Name of the model.
            model_meta: The model metadata.
            model_blobs_dir_path: Directory path to the whole model.

        Returns:
            The model object as a custom model.
        """
        from snowflake.ml.model import custom_model

        def _create_custom_model(
            raw_model: Union["sklearn.base.BaseEstimator", "sklearn.pipeline.Pipeline"],
            model_meta: model_meta_api.ModelMetadata,
        ) -> Type[custom_model.CustomModel]:
            def fn_factory(
                raw_model: Union["sklearn.base.BaseEstimator", "sklearn.pipeline.Pipeline"],
                output_col_names: Sequence[str],
                target_method: str,
            ) -> Callable[[custom_model.CustomModel, pd.DataFrame], pd.DataFrame]:
                @custom_model.inference_api
                def fn(self: custom_model.CustomModel, X: pd.DataFrame) -> pd.DataFrame:
                    res = getattr(raw_model, target_method)(X)

                    if isinstance(res, list) and len(res) > 0 and isinstance(res[0], np.ndarray):
                        # In case of multi-output estimators, predict_proba(), decision_function(), etc., functions
                        # return a list of ndarrays. We need to concatenate them.
                        res = np.concatenate(res, axis=1)
                    return pd.DataFrame(res, columns=output_col_names)

                return fn

            type_method_dict = {}
            for target_method_name, sig in model_meta.signatures.items():
                type_method_dict[target_method_name] = fn_factory(
                    raw_model, [spec.name for spec in sig.outputs], target_method_name
                )

            _SKLModel = type(
                "_SKLModel",
                (custom_model.CustomModel,),
                type_method_dict,
            )

            return _SKLModel

        raw_model = _SKLModelHandler._load_model(name, model_meta, model_blobs_dir_path)
        _SKLModel = _create_custom_model(raw_model, model_meta)
        skl_model = _SKLModel(custom_model.ModelContext())

        return skl_model
