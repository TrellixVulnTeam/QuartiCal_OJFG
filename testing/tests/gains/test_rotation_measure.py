from copy import deepcopy
import pytest
import numpy as np
import dask.array as da
from quartical.calibration.calibrate import add_calibration_graph
from testing.utils.gains import apply_gains


@pytest.fixture(scope="module")
def opts(base_opts, solve_per):

    # Don't overwrite base config - instead create a copy and update.

    _opts = deepcopy(base_opts)

    _opts.input_ms.select_corr = [0, 1, 2, 3]
    _opts.solver.terms = ['G']
    _opts.solver.iter_recipe = [100]
    _opts.solver.propagate_flags = False
    _opts.solver.convergence_criteria = 1e-7
    _opts.solver.convergence_fraction = 1
    _opts.solver.threads = 2
    _opts.G.type = "rotation_measure"
    _opts.G.freq_interval = 0
    _opts.G.solve_per = solve_per

    return _opts


@pytest.fixture(scope="module")
def raw_xds_list(read_xds_list_output):
    # Only use the first xds. This overloads the global fixture.
    return read_xds_list_output[0][:1]


@pytest.fixture(scope="module")
def true_gain_list(predicted_xds_list, solve_per):

    gain_list = []

    for xds in predicted_xds_list:

        n_ant = xds.dims["ant"]
        utime_chunks = xds.UTIME_CHUNKS
        n_time = sum(utime_chunks)
        chan_chunks = xds.chunks["chan"]
        n_chan = xds.dims["chan"]
        n_dir = xds.dims["dir"]
        n_corr = xds.dims["corr"]

        lambda_sq = (299792458/xds.CHAN_FREQ.data)**2

        chunking = (utime_chunks, chan_chunks, n_ant, n_dir, n_corr)

        bound = 8 if solve_per == "array" else 10

        da.random.seed(0)
        rm = da.random.normal(size=(n_time, 1, n_ant, n_dir),
                              loc=0,
                              scale=bound)
        betas = rm * lambda_sq[None, :, None, None]

        gains = da.zeros((n_time, n_chan, n_ant, n_dir, n_corr),
                         chunks=chunking)
        gains[..., 0] = da.cos(betas)
        gains[..., 1] = -da.sin(betas)
        gains[..., 2] = da.sin(betas)
        gains[..., 3] = da.cos(betas)

        if solve_per == "array":
            gains = da.broadcast_to(gains[:, :, :1], gains.shape)

        gain_list.append(gains)

    return gain_list


@pytest.fixture(scope="module")
def corrupted_data_xds_list(predicted_xds_list, true_gain_list):

    corrupted_data_xds_list = []

    for xds, gains in zip(predicted_xds_list, true_gain_list):

        n_corr = xds.dims["corr"]

        ant1 = xds.ANTENNA1.data
        ant2 = xds.ANTENNA2.data
        time = xds.TIME.data

        row_inds = \
            time.map_blocks(lambda x: np.unique(x, return_inverse=True)[1])

        model = da.ones(xds.MODEL_DATA.data.shape, dtype=np.complex128)

        data = da.blockwise(apply_gains, ("rfc"),
                            model, ("rfdc"),
                            gains, ("rfadc"),
                            ant1, ("r"),
                            ant2, ("r"),
                            row_inds, ("r"),
                            n_corr, None,
                            align_arrays=False,
                            concatenate=True,
                            dtype=model.dtype)

        corrupted_xds = xds.assign({
            "DATA": ((xds.DATA.dims), data),
            "MODEL_DATA": ((xds.MODEL_DATA.dims), model),
            "FLAG": ((xds.FLAG.dims), da.zeros_like(xds.FLAG.data)),
            "WEIGHT": ((xds.WEIGHT.dims), da.ones_like(xds.WEIGHT.data))
            }
        )

        corrupted_data_xds_list.append(corrupted_xds)

    return corrupted_data_xds_list


@pytest.fixture(scope="module")
def add_calibration_graph_outputs(corrupted_data_xds_list,
                                  solver_opts, chain_opts, output_opts):
    # Overload this fixture as we need to use the corrupted xdss.
    return add_calibration_graph(corrupted_data_xds_list,
                                 solver_opts, chain_opts, output_opts)


# -----------------------------------------------------------------------------

def test_residual_magnitude(cmp_post_solve_data_xds_list):
    # Magnitude of the residuals should tend to zero.
    for xds in cmp_post_solve_data_xds_list:
        np.testing.assert_array_almost_equal(np.abs(xds._RESIDUAL.data), 0)


def test_solver_flags(cmp_post_solve_data_xds_list):
    # The solver should not add addiitonal flags to the test data.
    for xds in cmp_post_solve_data_xds_list:
        np.testing.assert_array_equal(xds._FLAG.data, xds.FLAG.data)


def test_gains(cmp_gain_xds_lod, true_gain_list):

    for solved_gain_dict, true_gain in zip(cmp_gain_xds_lod, true_gain_list):
        solved_gain_xds = solved_gain_dict["G"]
        solved_gain, solved_flags = da.compute(solved_gain_xds.gains.data,
                                               solved_gain_xds.gain_flags.data)
        true_gain = true_gain.compute().copy()

        true_gain[np.where(solved_flags)] = 0
        solved_gain[np.where(solved_flags)] = 0

        # To ensure the missing antenna handling doesn't render this test
        # useless, check that we have non-zero entries first.
        assert np.any(solved_gain), "All gains are zero!"
        np.testing.assert_array_almost_equal(true_gain, solved_gain)


def test_gain_flags(cmp_gain_xds_lod):

    for solved_gain_dict in cmp_gain_xds_lod:
        solved_gain_xds = solved_gain_dict["G"]
        solved_flags = solved_gain_xds.gain_flags.values

        frows, fchans, fants, fdir = np.where(solved_flags)

        # We know that these antennas are missing in the test data. No other
        # antennas should have flags.
        assert set(np.unique(fants)) == {18, 20}

# -----------------------------------------------------------------------------
