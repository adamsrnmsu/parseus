"""
Contains generic utility functions that are shared among the Parseus analytics classes
"""

import warnings
from typing import Iterable, Union

import numpy as np
import pandas as pd
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.utils.extmath import weighted_mode

from parseus.util.numerical import inv_robust_log_adjust, nanaverage, robust_log_adjust


def _nanquantile_2d(values, quantiles, sample_weight, values_sorted, axis):
    """Applies nanquantile to every row (axis=1/-1) or column (axis=0/-2) of a matrix"""
    if axis is None:
        raise ValueError("With a 2-dimensional array, you must specify an axis")
    if axis in (0, -2):
        values = values.T
    return np.array(
        [
            nanquantile(
                values[i], quantiles, sample_weight, values_sorted=values_sorted
            )
            for i in range(values.shape[0])
        ]
    ).T


def _check_1d_arguments(values, sample_weight, axis):
    """Validates the 'axis' and 'sample_weight' arguments and returns the weights as an array"""
    if axis is not None:
        raise ValueError(
            "'axis' can only be set when operating on a two-dimensional object"
        )
    if values.ndim > 2:
        raise ValueError("'values' must have no more than 2 dimensions")

    if sample_weight is None:
        return np.ones(len(values))
    if sample_weight.ndim != 1:
        raise ValueError("'sample_weight' must be one-dimensional")
    if sample_weight.size != values.size:
        raise ValueError(
            f"The shapes of 'values' {values.shape} and 'sample_weight' "
            f"{sample_weight.shape} do not match"
        )
    return np.array(sample_weight)


# https://stackoverflow.com/questions/21844024/weighted-percentile-using-numpy
# modified to work with NaNs and matrices
def nanquantile(
    values: np.ndarray,
    quantiles: Iterable[Union[float, int]],
    sample_weight=None,
    values_sorted=False,
    axis=None,
):
    """
    Very close to numpy.percentile, but supports weights

    :param np.ndarray values: The data to be analyzed
    :param quantiles: array-like with many quantiles needed, on the interval [0, 100]
    :type quantiles: iterable(float or int)
    :param sample_weight: array-like of the same length as `array`
    :param values_sorted: bool, if True, then will avoid sorting of initial array
    :param axis: The axis to measure along, similar to numpy's axis parameters. Only used when
        values.ndim == 2
    :return: numpy.ndarray with computed quantiles.
    :raises ValueError: When there is a mismatch between the 'axis' parameter and the dimensionality
        of the input or sample weights, or when the quantiles are invalid
    """
    values = np.array(values)
    quantiles = np.array(quantiles, dtype=float)

    if values.ndim == 2:
        return _nanquantile_2d(values, quantiles, sample_weight, values_sorted, axis)
    sample_weight = _check_1d_arguments(values, sample_weight, axis)

    if not np.all(quantiles >= 0) or not np.all(quantiles <= 100):
        raise ValueError("Quantiles should be in [0, 100]")
    quantiles = quantiles / 100

    # ditch the NaNs
    nan_mask = ~np.isnan(values)
    values = values[nan_mask]
    sample_weight = sample_weight[nan_mask]

    if not values_sorted:
        sorter = np.argsort(values)
        values = values[sorter]
        sample_weight = sample_weight[sorter]

    weighted_quantiles = np.cumsum(sample_weight) - 0.5 * sample_weight
    weighted_quantiles /= np.sum(sample_weight)
    return np.interp(quantiles, weighted_quantiles, values)


class WeightedSimpleImputer(SimpleImputer):
    """
    A weighted version of a SimpleImputer providing a 'fit' function with a weight parameter
    """

    def fit(self, X, y=None, sample_weight=None):
        """
        Fits the imputer with sample weights

        :param X: The data to fit, possibly containing NaNs
        :param y: Ignored
        :param sample_weight: Optionally specifies the sample weights for each sample in 'X'
        :raises ValueError: When the imputation strategy is unknown
        """
        X = getattr(X, "values", X)

        # This is required to force fit to work with 1D arrays
        if X.ndim == 1:
            X = X[:, np.newaxis]

        # Fit SimpleImputer
        self = super().fit(X, y)

        # No weights provided, just return fitted SimpleImputer
        if sample_weight is None:
            return self

        # Override SimpleImputer's strategies
        statistics = []
        for column_index in range(X.shape[1]):
            # find the impute value
            if self.strategy == "mean":
                value = nanaverage(X[:, column_index], weights=sample_weight)
            elif self.strategy == "most_frequent":
                value = weighted_mode(X[:, column_index], sample_weight)[0][0]
            elif self.strategy == "constant":
                value = self.fill_value
            else:
                raise ValueError(
                    f"Invalid weighted imputation strategy: {self.strategy}"
                )
            statistics.append(value)
        self.statistics_ = np.array(statistics)

        # train the indicator - here for completeness (at least a bit of it)
        if self.add_indicator:
            self.indicator_ = MissingIndicator(missing_values=self.missing_values)
            self.indicator_.fit(X)
        else:
            self.indicator_ = None

        return self

    def transform(self, X):
        """
        Transforms the input by imputing its missing values

        :param X: The original input to transform
        :return: The transformed input with the missing values imputed
        """
        X = getattr(X, "values", X)
        if X.ndim == 1:
            return super().transform(X[:, np.newaxis])[:, 0]
        return super().transform(X)

    def fit_transform(self, X, **kwargs):
        """Fit on the data, then transform."""
        return self.fit(X, **kwargs).transform(X)


def _broadcast_weights(X, sample_weight):
    """
    Lines the weights up with the columns of X.

    :return: The weights used for the mean, the weights used for the variance (broadcastable against
        X), and the sum of the weights
    :raises ValueError: When the dimensionality of X and the weights cannot be combined
    """
    if X.ndim == 2 and sample_weight.ndim == 2:
        # each column is weighted differently
        total = sample_weight.sum(axis=0, keepdims=True)
        return sample_weight, np.reshape(sample_weight, X.shape), total
    if X.ndim == 2 and sample_weight.ndim == 1:
        # each column is weighted the same
        return sample_weight, sample_weight[:, np.newaxis], sample_weight.sum()
    if X.ndim == 1 and (
        sample_weight.ndim == 1
        or (sample_weight.ndim == 2 and sample_weight.shape[-1] == 1)
    ):
        # there is only one column
        flat = sample_weight.flatten()
        return flat, flat, sample_weight.sum()
    raise ValueError(
        "Unknown dimensionality combination for the columns and weights: "
        f"column={X.ndim}, weights={sample_weight.ndim}"
    )


def _weighted_mean_and_std(X, sample_weight, with_std):
    """
    Computes the weighted mean and (when requested) weighted standard deviation of each column of X.

    :param X: A matrix, or a single column
    :param sample_weight: One weight per row, or one weight per cell for a matrix
    :param with_std: When False, the standard deviation is the constant 1.0
    :raises ValueError: When the dimensionality of X and the weights cannot be combined
    """
    mean_weights, variance_weights, total = _broadcast_weights(X, sample_weight)
    mean = np.average(X, axis=0, weights=mean_weights).flatten()
    if not with_std:
        return mean, 1.0
    return mean, np.sqrt(np.sum((X - mean) ** 2 * variance_weights, axis=0) / total)


class WeightedStandardScaler(StandardScaler):
    """
    A variant of the StandardScaler that uses sample weights
    """

    # pylint: disable=arguments-differ
    def fit(self, X, y=None, sample_weight=None):
        # pylint: disable=attribute-defined-outside-init
        """
        Fits the weighted standard scalar
        """
        X = getattr(X, "values", X)

        if sample_weight is None:
            return super().fit(X, y)

        sample_weight = getattr(sample_weight, "values", sample_weight)
        mean, std = _weighted_mean_and_std(X, sample_weight, self.with_std)

        if self.with_mean:
            self.mean_ = mean
        if self.with_std:
            self.scale_ = np.maximum(std, 1e-5)

        return self

    def transform(self, X, copy=None):
        """
        Transforms the data using the already-trained model
        """
        if isinstance(X, pd.DataFrame):
            result = super().transform(X, copy=copy)
            return pd.DataFrame(result, index=X.index, columns=X.columns)
        if X.ndim == 1:
            result = super().transform(X[:, np.newaxis], copy=copy).flatten()
        else:
            result = super().transform(X, copy=copy)
        return result


def _stack_padded(rows, pad_value, length):
    """
    Stacks ragged columns into a matrix (one column per row), padding the short ones.

    :param rows: The columns
    :param pad_value: Callable giving the padding for a column
    :param length: The length each column is padded to
    :return: The stacked matrix, or a single row of zeros when the first column is empty
    """
    if not list(rows[0]):
        return np.zeros((1, len(rows)))
    return np.stack(
        [list(row) + [pad_value(row)] * (length - len(row)) for row in rows], axis=1
    )


def _central_subset(X, sample_weight, quantile_range):
    """
    Selects, for each column of X, the values that lie within the quantile range

    :return: The padded values (one column per column of X) and the correspondingly padded sample
        weights (or None)
    """
    if sample_weight is not None:
        quantiles = nanquantile(X, quantile_range, sample_weight, axis=0)
    else:
        quantiles = np.nanpercentile(X, quantile_range, axis=0)

    indices = [
        list(
            np.where(
                np.logical_and(quantiles[0, i] <= X[:, i], X[:, i] <= quantiles[1, i])
            )[0]
        )
        for i in range(X.shape[1])
    ]
    columns = [X[index, i] for i, index in enumerate(indices)]
    max_len = max(map(len, columns))
    values = _stack_padded(columns, np.mean, max_len)
    if sample_weight is None:
        return values, None
    weights = [sample_weight[index] for index in indices]
    return values, _stack_padded(weights, lambda _: 0.0, max_len)


class RobustStandardScaler(RobustScaler):
    """
    A variant of the RobustScaler that instead of measuring the scale by the quantiles, scales like
    a normal
    distribution (by the mean and variance) but discards the extreme percentiles
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._is_empty = False

    def _fit_empty(self):
        """Marks the scaler as fitted on empty input, which is passed through unchanged"""
        self._is_empty = True
        self.center_ = None
        self.scale_ = None
        self.center_after_ = None
        self.scale_after_ = None
        return self

    def fit(self, X, y=None, sample_weight=None):
        """
        Fits the robust standard scaler
        :raises ValueError: When the quantile range is invalid
        """
        q_min, q_max = self.quantile_range
        if not 0 <= q_min <= q_max <= 100:
            raise ValueError(f"Invalid quantile range: {self.quantile_range}")

        X = getattr(X, "values", X)
        if sample_weight is not None:
            sample_weight = getattr(sample_weight, "values", sample_weight)

        if X.shape[0] == 0 or X.shape[1] == 0:
            return self._fit_empty()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            values, subset_sample_weight = _central_subset(
                X, sample_weight, self.quantile_range
            )

        model = WeightedStandardScaler(
            copy=self.copy, with_mean=self.with_centering, with_std=self.with_scaling
        )
        model.fit(values, sample_weight=subset_sample_weight)

        if self.with_centering:
            self.center_ = model.mean_

        if self.with_scaling:
            self.scale_ = np.sqrt(model.scale_**2 + 1e-4)

        # now transform it again, so that after the robust log adjustment it has mean=0, stddev=1
        X = robust_log_adjust(super().transform(X))

        post_scale = WeightedStandardScaler(
            copy=self.copy, with_mean=self.with_centering, with_std=self.with_scaling
        )
        post_scale.fit(X, sample_weight=sample_weight)
        if self.with_centering:
            self.center_after_ = post_scale.mean_

        if self.with_scaling:
            self.scale_after_ = post_scale.scale_

        return self

    def transform(self, X):
        """
        Transforms the data using the robust log adjustment so that mean=0, stddev=1
        """
        X = getattr(X, "values", X)

        if self._is_empty:
            return X  # the input was empty so we'll assume this was too

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            X = robust_log_adjust(super().transform(X))
            if self.with_centering:
                X -= np.expand_dims(self.center_after_, axis=0)
            if self.with_scaling:
                X /= np.expand_dims(self.scale_after_, axis=0)
            return X

    def inverse_transform(self, X):
        """
        Performs the inverse transformation
        """
        X = getattr(X, "values", X)

        if self._is_empty:
            return X  # the input was empty so we'll assume this was too

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            if hasattr(self, "scale_after_"):
                X = X * np.expand_dims(self.scale_after_, axis=0)
            if hasattr(self, "center_after_"):
                X = X + np.expand_dims(self.center_after_, axis=0)
            return super().inverse_transform(inv_robust_log_adjust(X))


class LabeledDataFrame(pd.DataFrame):
    """
    Represents a variant on a DataFrame that includes a `label` attribute to be used in sequence
    labeling tasks. Note
    that this attribute will not be serialized like the other traditional DataFrame attributes and
    data.
    """

    def __init__(self, *args, label=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.label = label

    @classmethod
    def from_full_dataframe(cls, full_dataframe: pd.DataFrame, label_column: str):
        """
        Extracts a column from the DataFrame to use as the label for a new LabeledDataFrame

        :param full_dataframe: The DataFrame, including a constant column containing the sequence's
            label
        :param label_column: The name of the column containing the labels. This must be present in
            `full_dataframe` and have only one unique value.
        :return: The new LabeledDataFrame, with the label attribute set and the label column removed
        """
        if full_dataframe.shape[0] == 0:
            raise ValueError(
                "The DataFrame is empty, so its label could not be inferred"
            )
        if label_column not in full_dataframe.columns:
            raise ValueError(
                f"The column '{label_column}' is not present in the DataFrame"
            )
        values = set(full_dataframe[label_column].values)
        if len(values) != 1:
            raise ValueError(
                f"Multiple values were found in '{label_column}': {values}"
            )
        return cls(
            full_dataframe[
                [column for column in full_dataframe.columns if column != label_column]
            ],
            label=list(values)[0],
        )


class repeatable_map:
    """
    Similar to the built-in map() class except this iterator can be repeated
    """

    def __init__(self, function, iterable):
        self.function = function
        self.iterable = iterable

    def __iter__(self):
        return map(self.function, self.iterable)

    def __len__(self):
        return len(self.iterable)

    def __getitem__(self, item):
        return self.function(self.iterable[item])
