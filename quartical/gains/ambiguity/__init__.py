from quartical.gains.gain import Gain, gain_spec_tup, param_spec_tup
from quartical.gains.ambiguity.kernel import ambiguity_solver, ambiguity_args
import numpy as np


class Ambiguity(Gain):

    solver = ambiguity_solver
    term_args = ambiguity_args

    def __init__(self, term_name, term_opts, data_xds, coords, tipc, fipc):

        Gain.__init__(self, term_name, term_opts, data_xds, coords, tipc, fipc)

        self.n_param = 1  # This term only makes sense in a 2x2 chain.
        self.gain_chunk_spec = gain_spec_tup(self.n_tipc_g,
                                             self.n_fipc_g,
                                             (self.n_ant,),
                                             (self.n_dir,),
                                             (self.n_corr,))
        self.param_chunk_spec = param_spec_tup(self.n_tipc_g,  # Check!
                                               self.n_fipc_g,
                                               (self.n_ant,),
                                               (self.n_dir,),
                                               (self.n_param,))

        self.gain_axes = ("gain_t", "gain_f", "ant", "dir", "corr")
        self.param_axes = ("param_t", "param_f", "ant", "dir", "param")

    def make_xds(self):

        xds = Gain.make_xds(self)

        xds = xds.assign_coords({"param": np.array(["ambiguity"]),
                                 "param_t": self.gain_times,
                                 "param_f": self.gain_freqs})
        xds = xds.assign_attrs({"GAIN_SPEC": self.gain_chunk_spec,
                                "PARAM_SPEC": self.param_chunk_spec,
                                "GAIN_AXES": self.gain_axes,
                                "PARAM_AXES": self.param_axes})

        return xds
