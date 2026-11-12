"""Characterization tests for parseus.analytics.util.

The expected arrays were recorded from the implementation as it was before it
was split into smaller functions (the numerical results must not change).
"""

import warnings

import numpy as np
import pandas as pd
import pytest

from parseus.analytics.util import (
    LabeledDataFrame,
    RobustStandardScaler,
    WeightedSimpleImputer,
    WeightedStandardScaler,
    nanquantile,
    repeatable_map,
)

X1 = np.array([1.0, 2.0, 3.0, 4.0, 100.0, np.nan])
W1 = np.arange(1.0, 7.0)
X2 = np.array(
    [
        [1.0, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, np.nan, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [100.0, 95.0, 5.0],
    ]
)
CLEAN = np.nan_to_num(X2, nan=35.0)
W_ROWS = np.arange(1.0, 9.0)
W_CELLS = np.add.outer(np.arange(1.0, 9.0), [0.0, 0.5, 1.0])


def _quantile_cases():
    return {
        "nq_1d": nanquantile(X1, [0.0, 25.0, 50.0, 100.0]),
        "nq_1d_weighted": nanquantile(X1, [10.0, 90.0], sample_weight=W1),
        "nq_axis0": nanquantile(X2, [25.0, 75.0], axis=0),
        "nq_axis1": nanquantile(X2, [25.0, 75.0], axis=1),
        "nq_axis_neg2": nanquantile(X2, [50.0], axis=-2),
        "nq_axis0_weighted": nanquantile(
            CLEAN[:, :2], [50.0], sample_weight=W_ROWS, axis=0
        ),
        "nq_sorted": nanquantile(np.array([1.0, 2.0, 3.0]), [50.0], values_sorted=True),
    }


def _imputer_cases():
    out = {}
    for strategy in ("mean", "most_frequent", "constant"):
        imputer = WeightedSimpleImputer(strategy=strategy, fill_value=7.0)
        imputer.fit(np.round(X2 / 3), sample_weight=W_ROWS)
        out[f"imp_{strategy}_stats"] = imputer.statistics_
        out[f"imp_{strategy}_out"] = imputer.transform(np.round(X2 / 3))
    plain = WeightedSimpleImputer(strategy="mean").fit(X2)
    out["imp_unweighted_stats"] = plain.statistics_
    out["imp_1d"] = WeightedSimpleImputer().fit_transform(X1)
    out["imp_frame"] = (
        WeightedSimpleImputer().fit(pd.DataFrame(X2)).transform(pd.DataFrame(X2))
    )
    return out


def _scaler_cases():
    out = {}
    layouts = {
        "cells": (CLEAN, W_CELLS),
        "rows": (CLEAN, W_ROWS),
        "one_col": (CLEAN[:, 1], W_ROWS),
        "one_col_2d_weights": (CLEAN[:, 1], W_ROWS[:, None]),
    }
    for name, (data, weights) in layouts.items():
        for centre, scale in ((True, True), (True, False), (False, True)):
            scaler = WeightedStandardScaler(with_mean=centre, with_std=scale)
            scaler.fit(data, sample_weight=weights)
            key = f"ws_{name}_{int(centre)}{int(scale)}"
            for attr in ("mean_", "scale_"):
                value = getattr(scaler, attr, None)
                out[f"{key}_{attr}"] = np.asarray(np.nan if value is None else value)
            out[key + "_out"] = np.asarray(scaler.transform(data))
    plain = WeightedStandardScaler().fit(CLEAN)
    out["ws_plain_mean"] = plain.mean_
    out["ws_plain_scale"] = plain.scale_
    return out


def _robust_cases():
    out = {}
    options = {
        "default": {},
        "range": {"quantile_range": (10.0, 90.0)},
        "nocentre": {"with_centering": False},
        "noscale": {"with_scaling": False},
    }
    for name, kwargs in options.items():
        for weighted in (False, True):
            weights = W_ROWS if weighted else None
            scaler = RobustStandardScaler(**kwargs).fit(CLEAN, sample_weight=weights)
            transformed = scaler.transform(CLEAN)
            key = f"rs_{name}_{int(weighted)}"
            for attr in ("center_", "scale_", "center_after_", "scale_after_"):
                out[f"{key}_{attr}"] = np.asarray(getattr(scaler, attr, np.nan))
            out[key + "_out"] = transformed
            out[key + "_inv"] = scaler.inverse_transform(transformed)
    constant = np.c_[np.ones(8), np.arange(8.0)]
    for weights in (None, W_ROWS):
        scaler = RobustStandardScaler().fit(constant, sample_weight=weights)
        out[f"rs_const_{weights is None}_after"] = scaler.scale_after_
        out[f"rs_const_{weights is None}_out"] = scaler.transform(constant)
    out["rs_frame"] = RobustStandardScaler().fit_transform(pd.DataFrame(CLEAN))
    return out


def _all_cases():
    return {
        **_quantile_cases(),
        **_imputer_cases(),
        **_scaler_cases(),
        **_robust_cases(),
    }


nan = np.nan
EXPECTED = {
    "imp_1d": [1.0, 2.0, 3.0, 4.0, 100.0, 22.0],
    "imp_constant_out": [
        [0.0, 3.0, 2.0],
        [1.0, 7.0, 0.0],
        [1.0, 7.0, 1.0],
        [1.0, 13.0, 0.0],
        [2.0, 17.0, 3.0],
        [2.0, 20.0, 1.0],
        [2.0, 23.0, 2.0],
        [33.0, 32.0, 2.0],
    ],
    "imp_constant_stats": [7.0, 7.0, 7.0],
    "imp_frame": [
        [1.0, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, 49.285714285714285, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [100.0, 95.0, 5.0],
    ],
    "imp_mean_out": [
        [0.0, 3.0, 2.0],
        [1.0, 7.0, 0.0],
        [1.0, 20.939393939393938, 1.0],
        [1.0, 13.0, 0.0],
        [2.0, 17.0, 3.0],
        [2.0, 20.0, 1.0],
        [2.0, 23.0, 2.0],
        [33.0, 32.0, 2.0],
    ],
    "imp_mean_stats": [8.583333333333334, 20.939393939393938, 1.5555555555555556],
    "imp_most_frequent_out": [
        [0.0, 3.0, 2.0],
        [1.0, 7.0, 0.0],
        [1.0, 32.0, 1.0],
        [1.0, 13.0, 0.0],
        [2.0, 17.0, 3.0],
        [2.0, 20.0, 1.0],
        [2.0, 23.0, 2.0],
        [33.0, 32.0, 2.0],
    ],
    "imp_most_frequent_stats": [2.0, 32.0, 2.0],
    "imp_unweighted_stats": [16.0, 49.285714285714285, 4.125],
    "nq_1d": [1.0, 1.75, 3.0, 100.0],
    "nq_1d_weighted": [1.6666666666666667, 100.0],
    "nq_axis0": [[2.5, 25.0, 1.5], [6.5, 67.5, 5.5]],
    "nq_axis0_weighted": [[6.0, 60.0]],
    "nq_axis1": [
        [2.0, 1.25, 3.0, 1.75, 6.0, 3.0, 6.25, 27.499999999999996],
        [
            8.75,
            15.499999999999998,
            4.0,
            30.999999999999996,
            39.75,
            46.49999999999999,
            54.24999999999999,
            98.75,
        ],
    ],
    "nq_axis_neg2": [[4.5, 50.0, 4.5]],
    "nq_sorted": [2.0],
    "rs_const_False_after": [1.0000000000000001e-05, 2.4377218944841728e00],
    "rs_const_False_out": [
        [0.0, -2.366431913239847],
        [0.0, -1.8593393604027366],
        [0.0, -1.3522468075656267],
        [0.0, -0.8451542547285167],
        [0.0, -0.33806170189140666],
        [0.0, 0.16903085094570333],
        [0.0, 0.6761234037828133],
        [0.0, 1.1832159566199234],
    ],
    "rs_const_True_after": [1.0, 2.8980435150296824],
    "rs_const_True_out": [
        [0.0, -1.5275252316519468],
        [0.0, -1.0910894511799618],
        [0.0, -0.6546536707079772],
        [0.0, -0.21821789023599233],
        [0.0, 0.21821789023599242],
        [0.0, 0.6546536707079772],
        [0.0, 1.0910894511799618],
        [0.0, 1.5275252316519468],
    ],
    "rs_default_0_center_": [4.5, 46.25, 4.0],
    "rs_default_0_center_after_": [
        1.4007251181510165,
        0.1301888403699465,
        0.10205867071697255,
    ],
    "rs_default_0_inv": [
        [1.0000000000000009, 10.0, 5.0],
        [2.0000000000000004, 19.999999999999993, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [99.99999999999982, 95.0, 5.0],
    ],
    "rs_default_0_out": [
        [-0.8768381589688066, -1.4569287935358963, 0.34035164682842056],
        [-0.7037593535619675, -1.0684144485929907, -1.215541595815788],
        [-0.5306805481551283, -0.48564293117863205, -0.048621663832631515],
        [-0.3576017427482892, -0.2913857587071792, -1.215541595815788],
        [-0.1845229373414501, 0.09712858623572644, 1.8962448894726294],
        [-0.01144413193461095, 0.4856429311786321, -0.8265682851547358],
        [0.1616346734722282, 0.8741572761215377, 0.7293249574894727],
        [2.5032121992380247, 1.8454431384788017, 0.34035164682842056],
    ],
    "rs_default_0_scale_": [1.1180787092150535, 9.601437392390787, 1.2247856955402443],
    "rs_default_0_scale_after_": [
        5.167538647822308,
        2.6807522978659337,
        2.0990369862348848,
    ],
    "rs_default_1_center_": [6.111111111111111, 61.111111111111114, 3.8333333333333335],
    "rs_default_1_center_after_": [
        2.1924765704981266,
        -0.08585028686537852,
        0.49612519969537494,
    ],
    "rs_default_1_inv": [
        [1.0, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [100.00000000000001, 95.0, 5.0],
    ],
    "rs_default_1_out": [
        [-1.2274739278624898, -2.1300308210022654, 0.19738550848793066],
        [-1.0491849389831502, -1.7075453689026423, -1.381698559415515],
        [-0.8708959501038107, -1.0738171907532081, -0.1973855084879307],
        [-0.6926069612244712, -0.8625744647033967, -1.381698559415515],
        [-0.5143179723451315, -0.44008901260377375, 1.776469576391376],
        [-0.336028983465792, -0.01760356050415091, -0.9869275424396535],
        [-0.1577399945864525, 0.4048818915954719, 0.592156525463792],
        [1.8001129032079606, 1.461095521844529, 0.19738550848793066],
    ],
    "rs_default_1_scale_": [[0.8089629087012091, 8.08901716931248, 1.3437468346215946]],
    "rs_default_1_scale_after_": [
        6.9334097767388405,
        2.926122366386085,
        1.8851125526992942,
    ],
    "rs_frame": [
        [-0.8768381589688065, -1.4569287935358963, 0.34035164682842056],
        [-0.7037593535619674, -1.0684144485929907, -1.215541595815788],
        [-0.5306805481551282, -0.48564293117863205, -0.04862166383263154],
        [-0.35760174274828915, -0.2913857587071792, -1.215541595815788],
        [-0.18452293734145, 0.09712858623572644, 1.8962448894726294],
        [-0.011444131934610904, 0.4856429311786321, -0.8265682851547358],
        [0.16163467347222824, 0.8741572761215377, 0.7293249574894727],
        [2.5032121992380247, 1.8454431384788017, 0.34035164682842056],
    ],
    "rs_nocentre_0_center_": nan,
    "rs_nocentre_0_center_after_": nan,
    "rs_nocentre_0_inv": [
        [1.0, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 49.99999999999999, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [100.00000000000009, 95.0, 5.0],
    ],
    "rs_nocentre_0_out": [
        [0.22658612914526213, 0.38851434494290565, 1.944866553305261],
        [0.45317225829052427, 0.7770286898858113, 0.3889733106610521],
        [0.6797583874357863, 1.3598002073001698, 1.5558932426442085],
        [0.9063445165810485, 1.5540573797716226, 0.3889733106610521],
        [1.1329306457263106, 1.9425717247145282, 3.5007597959494694],
        [1.3595167748715726, 2.331086069657434, 0.7779466213221042],
        [1.5861029040168348, 2.7196004146003396, 2.333839863966313],
        [3.6449456882349414, 3.6908862769576034, 1.944866553305261],
    ],
    "rs_nocentre_0_scale_": [1.1180787092150535, 9.601437392390787, 1.2247856955402443],
    "rs_nocentre_0_scale_after_": [
        3.9472469891807567,
        2.6807522978659337,
        2.0990369862348848,
    ],
    "rs_nocentre_1_center_": nan,
    "rs_nocentre_1_center_after_": nan,
    "rs_nocentre_1_inv": [
        [1.0, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 5.999999999999999],
        [100.00000000000009, 95.0, 5.0],
    ],
    "rs_nocentre_1_out": [
        [0.30681638367142977, 0.45883523056603104, 1.973855084879307],
        [0.6136327673428595, 0.9176704611320621, 0.39477101697586137],
        [0.9204491510142894, 1.6059233069811087, 1.5790840679034455],
        [1.227265534685719, 1.8353409222641242, 0.39477101697586137],
        [1.534081918357149, 2.294176152830155, 3.5529391527827525],
        [1.8408983020285787, 2.7530113833961862, 0.7895420339517227],
        [2.1477146857000085, 3.2118466139622175, 2.368626101855168],
        [3.6589052819123165, 4.086216878355671, 1.973855084879307],
    ],
    "rs_nocentre_1_scale_": [
        [0.8089629087012091, 8.08901716931248, 1.3437468346215946]
    ],
    "rs_nocentre_1_scale_after_": [
        4.028958961672305,
        2.6943095222578712,
        1.8851125526992942,
    ],
    "rs_noscale_0_center_": [4.5, 46.25, 4.0],
    "rs_noscale_0_center_after_": [1.3700180517422291, -0.19147479235877896, 0.125],
    "rs_noscale_0_inv": [
        [1.0, 10.000000000000014, 5.0],
        [2.0, 19.999999999999982, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 69.99999999999999, 6.0],
        [99.99999999999994, 95.0, 5.0],
    ],
    "rs_noscale_0_out": [
        [-4.870018051742229, -13.113578728750474, 0.875],
        [-3.870018051742229, -12.65633735111859, -3.125],
        [-2.870018051742229, -10.61945542385755, -0.125],
        [-1.8700180517422291, -6.058525207641221, -3.125],
        [-0.8700180517422291, 3.941474792358779, 4.875],
        [0.12998194825777087, 11.749619410405328, -2.125],
        [1.1299819482577709, 12.882717875144607, 1.875],
        [13.090126362195605, 13.87408463345912, 0.875],
    ],
    "rs_noscale_0_scale_": nan,
    "rs_noscale_0_scale_after_": nan,
    "rs_noscale_1_center_": [6.111111111111111, 61.111111111111114, 3.8333333333333335],
    "rs_noscale_1_center_after_": [
        2.3449898495425363,
        -0.6010982722652112,
        0.6666666666666665,
    ],
    "rs_noscale_1_inv": [
        [1.0, 9.999999999999993, 5.0],
        [2.0, 20.00000000000002, 1.0],
        [3.0000000000000004, 35.00000000000001, 4.0],
        [4.0, 39.99999999999999, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [99.99999999999997, 95.0, 5.0],
    ],
    "rs_noscale_1_out": [
        [-7.456100960653647, -13.139213355480996, 0.5],
        [-6.456100960653647, -12.868103838511, -3.5],
        [-5.456100960653647, -12.238629752812198, -0.5],
        [-4.456100960653647, -11.893025032627714, -3.5],
        [-3.456100960653647, -10.14611612956501, 4.5],
        [-2.456100960653647, -0.5100128388459031, -2.5],
        [-1.456100960653647, 9.489987161154097, 1.5],
        [12.096353362287765, 13.815519746784032, 0.5],
    ],
    "rs_noscale_1_scale_": nan,
    "rs_noscale_1_scale_after_": nan,
    "rs_range_0_center_": [4.5, 45.83333333333333, 3.4285714285714284],
    "rs_range_0_center_after_": [
        1.4657568645075476,
        0.10952884198624008,
        0.36335610069711405,
    ],
    "rs_range_0_inv": [
        [1.0000000000000009, 10.0, 5.0],
        [1.9999999999999996, 20.000000000000004, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [6.0, 60.0, 1.9999999999999998],
        [7.0, 70.0, 6.0],
        [99.99999999999997, 95.0, 5.0],
    ],
    "rs_range_0_out": [
        [-0.7569233274696296, -1.456928793535896, 0.3403516468284206],
        [-0.6268143755990787, -1.0684144485929905, -1.215541595815788],
        [-0.4967054237285275, -0.485642931178632, -0.04862166383263153],
        [-0.36659647185797645, -0.29138575870717914, -1.215541595815788],
        [-0.23648751998742537, 0.09712858623572652, 1.8962448894726291],
        [-0.10637856811687428, 0.4856429311786322, -0.8265682851547359],
        [0.023730383753676813, 0.8741572761215378, 0.7293249574894728],
        [2.5661753030058354, 1.8454431384788024, 0.3403516468284206],
    ],
    "rs_range_0_scale_": [1.5811704525445702, 15.216692119104566, 1.9166557822820203],
    "rs_range_0_scale_after_": [
        4.860871402082543,
        1.6915026702914016,
        1.3413313433304368,
    ],
    "rs_range_1_center_": [28.333333333333332, 64.39393939393939, 3.774193548387097],
    "rs_range_1_center_after_": [
        -0.05479124752599304,
        -0.19422607296392988,
        0.3794478967254741,
    ],
    "rs_range_1_inv": [
        [0.9999999999999964, 10.0, 5.0],
        [2.0, 20.0, 1.0],
        [3.0, 35.0, 4.0],
        [4.0, 40.0, 1.0],
        [5.0, 50.0, 9.0],
        [5.9999999999999964, 60.0, 2.0],
        [7.0, 70.0, 6.0],
        [100.0, 95.0, 5.0],
    ],
    "rs_range_1_out": [
        [-0.6353254315469494, -2.1300308210022654, 0.19738550848793074],
        [-0.6100248612641062, -1.7075453689026425, -1.381698559415515],
        [-0.5847242909812631, -1.0738171907532084, -0.19738550848793068],
        [-0.55942372069842, -0.8625744647033968, -1.381698559415515],
        [-0.5341231504155769, -0.4400890126037738, 1.7764695763913765],
        [-0.5088225801327338, -0.017603560504150993, -0.9869275424396534],
        [-0.48352200984989063, 0.4048818915954719, 0.592156525463792],
        [1.8694310264545193, 1.461095521844529, 0.19738550848793074],
    ],
    "rs_range_1_scale_": [[40.55797819109694, 20.47754282717414, 1.9127960857772657]],
    "rs_range_1_scale_after_": [
        0.9745259142801505,
        1.1558737423220733,
        1.324299042867279,
    ],
    "ws_cells_01_mean_": nan,
    "ws_cells_01_out": [
        [0.02530057028284312, 0.413278055350309, 1.965304351858846],
        [0.05060114056568624, 0.826556110700618, 0.39306087037176923],
        [0.07590171084852936, 1.4464731937260815, 1.572243481487077],
        [0.10120228113137247, 1.653112221401236, 0.39306087037176923],
        [0.1265028514142156, 2.066390276751545, 3.537547833345923],
        [0.1518034216970587, 2.479668332101854, 0.7861217407435385],
        [0.17710399197990184, 2.892946387452163, 2.3583652222306153],
        [2.530057028284312, 3.9261415258279353, 1.965304351858846],
    ],
    "ws_cells_01_scale_": [
        [39.524800778033146, 24.196784393799106, 2.5441352100354555]
    ],
    "ws_cells_10_mean_": [26.11111111111111, 59.125, 4.431818181818182],
    "ws_cells_10_out": [
        [-25.11111111111111, -49.125, 0.5681818181818183],
        [-24.11111111111111, -39.125, -3.4318181818181817],
        [-23.11111111111111, -24.125, -0.43181818181818166],
        [-22.11111111111111, -19.125, -3.4318181818181817],
        [-21.11111111111111, -9.125, 4.568181818181818],
        [-20.11111111111111, 0.875, -2.4318181818181817],
        [-19.11111111111111, 10.875, 1.5681818181818183],
        [73.88888888888889, 35.875, 0.5681818181818183],
    ],
    "ws_cells_10_scale_": nan,
    "ws_cells_11_mean_": [26.11111111111111, 59.125, 4.431818181818182],
    "ws_cells_11_out": [
        [-0.6353254315469494, -2.030228446908393, 0.22333003998395984],
        [-0.6100248612641063, -1.6169503915580838, -1.348913441503117],
        [-0.5847242909812632, -0.9970333085326205, -0.16973083038780937],
        [-0.5594237206984201, -0.790394280857466, -1.348913441503117],
        [-0.5341231504155769, -0.37711622550715695, 1.7955735214710367],
        [-0.5088225801327338, 0.03616182984315204, -0.9558525711313478],
        [-0.4835220098498907, 0.449439885193461, 0.616390910355729],
        [1.8694310264545193, 1.4826350235692336, 0.22333003998395984],
    ],
    "ws_cells_11_scale_": [
        [39.524800778033146, 24.196784393799106, 2.5441352100354555]
    ],
    "ws_one_col_01_mean_": nan,
    "ws_one_col_01_out": [
        0.4224854520996229,
        0.8449709041992458,
        1.47869908234868,
        1.6899418083984916,
        2.1124272604981145,
        2.5349127125977375,
        2.95739816469736,
        4.013611794946417,
    ],
    "ws_one_col_01_scale_": 23.669454061206302,
    "ws_one_col_10_mean_": [60.416666666666664],
    "ws_one_col_10_out": [
        -50.416666666666664,
        -40.416666666666664,
        -25.416666666666664,
        -20.416666666666664,
        -10.416666666666664,
        -0.4166666666666643,
        9.583333333333336,
        34.583333333333336,
    ],
    "ws_one_col_10_scale_": nan,
    "ws_one_col_11_mean_": [60.416666666666664],
    "ws_one_col_11_out": [
        -2.1300308210022654,
        -1.7075453689026425,
        -1.0738171907532081,
        -0.8625744647033967,
        -0.44008901260377375,
        -0.017603560504150854,
        0.40488189159547205,
        1.4610955218445294,
    ],
    "ws_one_col_11_scale_": 23.669454061206302,
    "ws_one_col_2d_weights_01_mean_": nan,
    "ws_one_col_2d_weights_01_out": [
        0.4224854520996229,
        0.8449709041992458,
        1.47869908234868,
        1.6899418083984916,
        2.1124272604981145,
        2.5349127125977375,
        2.95739816469736,
        4.013611794946417,
    ],
    "ws_one_col_2d_weights_01_scale_": 23.669454061206302,
    "ws_one_col_2d_weights_10_mean_": [60.416666666666664],
    "ws_one_col_2d_weights_10_out": [
        -50.416666666666664,
        -40.416666666666664,
        -25.416666666666664,
        -20.416666666666664,
        -10.416666666666664,
        -0.4166666666666643,
        9.583333333333336,
        34.583333333333336,
    ],
    "ws_one_col_2d_weights_10_scale_": nan,
    "ws_one_col_2d_weights_11_mean_": [60.416666666666664],
    "ws_one_col_2d_weights_11_out": [
        -2.1300308210022654,
        -1.7075453689026425,
        -1.0738171907532081,
        -0.8625744647033967,
        -0.44008901260377375,
        -0.017603560504150854,
        0.40488189159547205,
        1.4610955218445294,
    ],
    "ws_one_col_2d_weights_11_scale_": 23.669454061206302,
    "ws_plain_mean": [16.0, 47.5, 4.125],
    "ws_plain_scale": [31.804087787578503, 25.739075352467502, 2.5708704751503917],
    "ws_rows_01_mean_": nan,
    "ws_rows_01_out": [
        [0.02530057028284312, 0.4224854520996229, 1.9738550848793068],
        [0.05060114056568624, 0.8449709041992458, 0.39477101697586137],
        [0.07590171084852936, 1.47869908234868, 1.5790840679034455],
        [0.10120228113137247, 1.6899418083984916, 0.39477101697586137],
        [0.1265028514142156, 2.1124272604981145, 3.552939152782752],
        [0.1518034216970587, 2.5349127125977375, 0.7895420339517227],
        [0.17710399197990184, 2.95739816469736, 2.368626101855168],
        [2.530057028284312, 4.013611794946417, 1.9738550848793068],
    ],
    "ws_rows_01_scale_": [39.524800778033146, 23.669454061206302, 2.533114025595111],
    "ws_rows_10_mean_": [26.11111111111111, 60.416666666666664, 4.5],
    "ws_rows_10_out": [
        [-25.11111111111111, -50.416666666666664, 0.5],
        [-24.11111111111111, -40.416666666666664, -3.5],
        [-23.11111111111111, -25.416666666666664, -0.5],
        [-22.11111111111111, -20.416666666666664, -3.5],
        [-21.11111111111111, -10.416666666666664, 4.5],
        [-20.11111111111111, -0.4166666666666643, -2.5],
        [-19.11111111111111, 9.583333333333336, 1.5],
        [73.88888888888889, 34.583333333333336, 0.5],
    ],
    "ws_rows_10_scale_": nan,
    "ws_rows_11_mean_": [26.11111111111111, 60.416666666666664, 4.5],
    "ws_rows_11_out": [
        [-0.6353254315469494, -2.1300308210022654, 0.19738550848793068],
        [-0.6100248612641063, -1.7075453689026425, -1.3816985594155147],
        [-0.5847242909812632, -1.0738171907532081, -0.19738550848793068],
        [-0.5594237206984201, -0.8625744647033967, -1.3816985594155147],
        [-0.5341231504155769, -0.44008901260377375, 1.776469576391376],
        [-0.5088225801327338, -0.017603560504150854, -0.9869275424396534],
        [-0.4835220098498907, 0.40488189159547205, 0.592156525463792],
        [1.8694310264545193, 1.4610955218445294, 0.19738550848793068],
    ],
    "ws_rows_11_scale_": [39.524800778033146, 23.669454061206302, 2.533114025595111],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_numerical_results_match_recorded_values(name):
    actual = _all_cases()[name]
    np.testing.assert_allclose(actual, np.array(EXPECTED[name]), rtol=1e-9, atol=1e-12)


def test_every_recorded_case_is_still_produced():
    assert set(_all_cases()) == set(EXPECTED)


def test_nanquantile_does_not_mutate_inputs():
    quantiles = np.array([25.0, 75.0])
    values = X1.copy()
    nanquantile(values, quantiles)
    np.testing.assert_array_equal(quantiles, [25.0, 75.0])
    np.testing.assert_array_equal(values, X1)


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda: nanquantile(X2, [1.0]), "must specify an axis"),
        (lambda: nanquantile(X1, [1.0], axis=0), "'axis' can only be set"),
        (lambda: nanquantile(np.zeros((2, 2, 2)), [1.0]), "no more than 2"),
        (
            lambda: nanquantile(X1, [1.0], sample_weight=np.ones((6, 1))),
            "one-dimensional",
        ),
        (lambda: nanquantile(X1, [1.0], sample_weight=np.ones(3)), "do not match"),
        (lambda: nanquantile(X1, [101.0]), "in \\[0, 100\\]"),
        (lambda: nanquantile(X1, [-1.0]), "in \\[0, 100\\]"),
    ],
)
def test_nanquantile_rejects_bad_arguments(call, message):
    with pytest.raises(ValueError, match=message):
        call()


def test_nanquantile_accepts_integer_quantiles():
    np.testing.assert_allclose(nanquantile(X1, [0, 50, 100]), [1.0, 3.0, 100.0])


def test_imputer_rejects_unknown_weighted_strategy():
    with pytest.raises(ValueError, match="Invalid weighted imputation strategy"):
        WeightedSimpleImputer(strategy="median").fit(X2, sample_weight=W_ROWS)


def test_imputer_indicator_only_trained_when_weighted():
    with_indicator = WeightedSimpleImputer(add_indicator=True)
    with_indicator.fit(X2, sample_weight=W_ROWS)
    assert with_indicator.indicator_.features == "missing-only"
    without = WeightedSimpleImputer(add_indicator=False).fit(X2, sample_weight=W_ROWS)
    assert without.indicator_ is None


def test_imputer_fit_returns_self_and_fit_transform_forwards_weights():
    imputer = WeightedSimpleImputer()
    assert imputer.fit(X2) is imputer
    result = WeightedSimpleImputer().fit_transform(X2, sample_weight=W_ROWS)
    assert not np.isnan(result).any()


def test_standard_scaler_rejects_unsupported_dimensions():
    with pytest.raises(ValueError, match="Unknown dimensionality"):
        WeightedStandardScaler().fit(np.zeros((3, 2, 2)), sample_weight=np.ones(3))


def test_standard_scaler_accepts_pandas_inputs_and_returns_frames():
    frame = pd.DataFrame(CLEAN[:, :2], columns=["a", "b"], index=range(10, 18))
    scaler = WeightedStandardScaler().fit(frame, sample_weight=pd.Series(W_ROWS))
    result = scaler.transform(frame)
    assert isinstance(result, pd.DataFrame)
    assert list(result.columns) == ["a", "b"]
    assert list(result.index) == list(range(10, 18))
    reference = WeightedStandardScaler().fit(CLEAN[:, :2], sample_weight=W_ROWS)
    np.testing.assert_allclose(result.values, reference.transform(CLEAN[:, :2]))


def test_standard_scaler_transforms_one_dimensional_input():
    scaler = WeightedStandardScaler().fit(CLEAN[:, 1], sample_weight=W_ROWS)
    result = scaler.transform(CLEAN[:, 1])
    assert result.shape == (8,)


def test_robust_scaler_rejects_invalid_quantile_range():
    with pytest.raises(ValueError, match="Invalid quantile range"):
        RobustStandardScaler(quantile_range=(90.0, 10.0)).fit(CLEAN)


@pytest.mark.parametrize("shape", [(0, 2), (3, 0)])
def test_robust_scaler_passes_empty_input_through(shape):
    scaler = RobustStandardScaler().fit(np.zeros(shape))
    empty = np.zeros(shape)
    assert scaler.center_ is None and scaler.scale_ is None
    assert scaler.transform(empty) is empty
    assert scaler.inverse_transform(empty) is empty


def test_robust_scaler_accepts_pandas_weights():
    expected = RobustStandardScaler().fit(CLEAN, sample_weight=W_ROWS).transform(CLEAN)
    actual = (
        RobustStandardScaler()
        .fit(pd.DataFrame(CLEAN), sample_weight=pd.Series(W_ROWS))
        .transform(pd.DataFrame(CLEAN))
    )
    np.testing.assert_allclose(actual, expected)


def test_robust_scaler_does_not_leak_runtime_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        scaler = RobustStandardScaler().fit(np.c_[np.ones(8), np.arange(8.0)])
        scaler.inverse_transform(scaler.transform(np.c_[np.ones(8), np.arange(8.0)]))


def test_labeled_dataframe_keeps_label():
    assert LabeledDataFrame({"a": [1, 2]}, label="x").label == "x"
    assert LabeledDataFrame({"a": [1]}).label is None


def test_labeled_dataframe_from_full_dataframe():
    frame = pd.DataFrame({"a": [1, 2], "b": [3, 4], "label": ["x", "x"]})
    result = LabeledDataFrame.from_full_dataframe(frame, "label")
    assert isinstance(result, LabeledDataFrame)
    assert result.label == "x"
    assert list(result.columns) == ["a", "b"]


@pytest.mark.parametrize(
    "frame, column, message",
    [
        (pd.DataFrame({"label": []}), "label", "is empty"),
        (pd.DataFrame({"a": [1]}), "label", "is not present"),
        (pd.DataFrame({"label": ["x", "y"]}), "label", "Multiple values"),
    ],
)
def test_labeled_dataframe_rejects_bad_label_column(frame, column, message):
    with pytest.raises(ValueError, match=message):
        LabeledDataFrame.from_full_dataframe(frame, column)


def test_repeatable_map_can_be_iterated_repeatedly():
    mapped = repeatable_map(lambda value: value * 2, [1, 2, 3])
    assert list(mapped) == [2, 4, 6]
    assert list(mapped) == [2, 4, 6]
    assert len(mapped) == 3
    assert mapped[1] == 4


def test_robust_scaler_with_an_all_nan_first_column():
    data = np.c_[np.full(8, np.nan), np.arange(8.0)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        scaler = RobustStandardScaler().fit(data)
        result = scaler.transform(data)
    np.testing.assert_allclose(scaler.center_, [0.0, 0.0])
    np.testing.assert_allclose(scaler.scale_, [1.00005, 1.00005])
    np.testing.assert_allclose(scaler.scale_after_[1], 2.29117329, rtol=1e-7)
    assert np.isnan(result[:, 0]).all()
    np.testing.assert_allclose(result[:2, 1], [-1.52752523, -1.09108945], rtol=1e-7)


def test_robust_scaler_weighted_fit_of_an_all_nan_column_fails():
    data = np.c_[np.full(8, np.nan), np.arange(8.0)]
    with pytest.raises(ValueError, match="sample points is empty"):
        RobustStandardScaler().fit(data, sample_weight=W_ROWS)
