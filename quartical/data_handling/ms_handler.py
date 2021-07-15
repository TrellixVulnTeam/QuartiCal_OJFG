# -*- coding: utf-8 -*-
import dask.array as da
import numpy as np
from daskms import xds_from_ms, xds_from_table, xds_to_table
from dask.graph_manipulation import clone
from loguru import logger
from quartical.weights.weights import initialize_weights
from quartical.flagging.flagging import initialise_flags
from quartical.data_handling.chunking import compute_chunking
from quartical.data_handling.bda import process_bda_input, process_bda_output
from quartical.data_handling.selection import filter_xds_list


CORR_TYPES = {
  5: "RR",
  6: "RL",
  7: "LR",
  8: "LL",
  9: "XX",
  10: "XY",
  11: "YX",
  12: "YY",
}


def read_xds_list(model_columns, ms_opts):
    """Reads a measurement set and generates a list of xarray data sets.

    Args:
        model_columns: A list of strings containing additional model columns to
            be read.
        ms_opts: A MSInputs configuration object.

    Returns:
        data_xds_list: A list of appropriately chunked xarray datasets.
    """

    antenna_xds = xds_from_table(ms_opts.path + "::ANTENNA")[0]

    n_ant = antenna_xds.dims["row"]

    logger.info("Antenna table indicates {} antennas were present for this "
                "observation.", n_ant)

    # Determine the number/type of correlations present in the measurement set.
    pol_xds = xds_from_table(ms_opts.path + "::POLARIZATION")[0]

    try:
        corr_types = [CORR_TYPES[ct] for ct in pol_xds.CORR_TYPE.values[0]]
    except KeyError:
        raise KeyError("Data contains unsupported correlation products.")

    n_corr = len(corr_types)

    if n_corr not in (1, 2, 4):
        raise ValueError(f"Measurement set contains {n_corr} correlations - "
                         f"this is not supported.")

    logger.info(f"Polarization table indicates {n_corr} correlations are "
                f"present in the measurement set - {corr_types}.")

    # Determine the phase direction from the measurement set. TODO: This will
    # probably need to be done on a per xds basis. Can probably be accomplished
    # by merging the field xds grouped by DDID into data grouped by DDID.

    field_xds = xds_from_table(ms_opts.path + "::FIELD")[0]
    phase_dir = np.squeeze(field_xds.PHASE_DIR.values)
    field_names = field_xds.NAME.values

    logger.info("Field table indicates phase centre is at ({} {}).",
                phase_dir[0], phase_dir[1])

    # Determine all the chunking in time, row and channel.
    chunking_info = compute_chunking(ms_opts, compute=True)

    utime_chunking_per_data_xds = chunking_info[0]
    chunking_per_data_xds = chunking_info[1]
    chunking_per_spw_xds = chunking_info[2]

    # Once we have determined the row chunks from the indexing columns, we set
    # up an xarray data set for the data. Note that we will reload certain
    # indexing columns so that they are consistent with the chunking strategy.

    columns = ("TIME", "INTERVAL", "ANTENNA1", "ANTENNA2",
               "FLAG", "FLAG_ROW", "UVW")
    columns += (ms_opts.data_column,)
    columns += (ms_opts.weight_column,) if ms_opts.weight_column else ()
    columns += (*model_columns,)

    available_columns = list(xds_from_ms(ms_opts.path)[0].keys())
    assert all(c in available_columns for c in columns), \
           f"One or more columns in: {columns} is not present in the data."

    schema = {cn: {'dims': ('chan', 'corr')} for cn in model_columns}

    data_xds_list = xds_from_ms(
        ms_opts.path,
        columns=columns,
        index_cols=("TIME",),
        group_cols=ms_opts.group_by,
        taql_where="ANTENNA1 != ANTENNA2",
        chunks=chunking_per_data_xds,
        table_schema=["MS", {**schema}])

    spw_xds_list = xds_from_table(
        ms_opts.path + "::SPECTRAL_WINDOW",
        group_cols=["__row__"],
        columns=["CHAN_FREQ", "CHAN_WIDTH"],
        chunks=chunking_per_spw_xds
    )

    # Preserve a copy of the xds_list prior to any BDA/assignment. Necessary
    # for undoing BDA.
    ref_xds_list = data_xds_list if ms_opts.is_bda else None

    # BDA data needs to be processed into something more manageable. TODO:
    # This is almost certainly broken. Needs test cases.
    if ms_opts.is_bda:
        data_xds_list, utime_chunking_per_data_xds = process_bda_input(
            data_xds_list,
            spw_xds_list,
            ms_opts.weight_column
        )

    _data_xds_list = []

    for xds_ind, xds in enumerate(data_xds_list):
        # Add coordinates to the xarray datasets.
        _xds = xds.assign_coords({"corr": corr_types,
                                  "chan": np.arange(xds.dims["chan"]),
                                  "ant": np.arange(n_ant)})

        # Add the actual channel frequecies to the xds - this is in preparation
        # for solvers which require this information. Also adds the antenna
        # names which will be useful when reference antennas are required.

        chan_freqs = clone(spw_xds_list[xds.DATA_DESC_ID].CHAN_FREQ.data)
        chan_widths = clone(spw_xds_list[xds.DATA_DESC_ID].CHAN_WIDTH.data)
        ant_names = clone(antenna_xds.NAME.data)

        _xds = _xds.assign({"CHAN_FREQ": (("chan",), chan_freqs[0]),
                            "CHAN_WIDTH": (("chan",), chan_widths[0]),
                            "ANT_NAME": (("ant",), ant_names)})

        # Add an attribute to the xds on which we will store the names of
        # fields which must be written to the MS. Also add the attribute which
        # stores the unique time chunking per xds. We have to convert the
        # chunking to python integers to avoid problems with serialization.

        utime_chunks = tuple(map(int, utime_chunking_per_data_xds[xds_ind]))
        field_id = getattr(xds, "FIELD_ID", None)
        field_name = "UNKNOWN" if field_id is None else field_names[field_id]

        _xds = _xds.assign_attrs({"WRITE_COLS": (),
                                  "UTIME_CHUNKS": utime_chunks,
                                  "FIELD_NAME": field_name})

        _data_xds_list.append(_xds)

    data_xds_list = _data_xds_list

    # Filter out fields/ddids which we are not interested in. Also select out
    # correlations. TODO: Does this type of selection/filtering belong here?

    data_xds_list = filter_xds_list(data_xds_list,
                                    ms_opts.select_fields,
                                    ms_opts.select_ddids)

    # TODO: Do we want to select on index or corr_type?
    if ms_opts.select_corr:
        try:
            data_xds_list = [xds.isel(corr=ms_opts.select_corr)
                             for xds in data_xds_list]
        except KeyError:
            raise KeyError(f"--input-ms-select-corr attempted to select "
                           f"correlations not present in the data - this MS "
                           f"contains {n_corr} correlations. User "
                           f"attempted to select {ms_opts.select_corr}.")

    return data_xds_list, ref_xds_list


def write_xds_list(xds_list, ref_xds_list, ms_path, output_opts):
    """Writes fields spicified in the WRITE_COLS attribute to the MS.

    Args:
        xds_list: A list of xarray datasets.
        ref_xds_list: A list of reference xarray.Dataset objects.
        ms_path: Path to input/output MS.
        output_opts: An Outputs configuration object.

    Returns:
        write_xds_list: A list of xarray datasets indicating success of writes.
    """

    # If we selected some correlations, we need to be sure that whatever we
    # attempt to write back to the MS is still consistent. This does this using
    # the magic of reindex. TODO: Check whether it would be better to let
    # dask-ms handle this. This also might need some further consideration,
    # as the fill_value might cause problems.

    pol_xds = xds_from_table(ms_path + "::POLARIZATION")[0]
    corr_types = [CORR_TYPES[ct] for ct in pol_xds.CORR_TYPE.values[0]]
    ms_n_corr = len(corr_types)

    _xds_list = []

    for xds in xds_list:

        _, u_corr_ind = np.unique(xds.corr.values, return_index=True)

        # Check for duplicate correlations - select out first occurence.
        if u_corr_ind.size < xds.corr.values.size:
            xds = xds.isel(corr=u_corr_ind)

        # If the xds has fewer correlations than the measurement set, reindex.
        if xds.dims["corr"] < ms_n_corr:
            xds = xds.reindex(corr=corr_types, fill_value=0)

        _xds_list.append(xds)

    xds_list = _xds_list

    output_cols = tuple(set([cn for xds in xds_list for cn in xds.WRITE_COLS]))

    if output_opts.products:
        # Drop variables from columns we intend to overwrite.
        xds_list = [xds.drop_vars(output_opts.columns, errors="ignore")
                    for xds in xds_list]

        vis_prod_map = {"residual": "_RESIDUAL",
                        "corrected_residual": "_CORRECTED_RESIDUAL",
                        "corrected_data": "_CORRECTED_DATA"}
        n_vis_prod = len(output_opts.products)

        # Rename QuartiCal's underscore prefixed results so that they will be
        # written to the appropriate column.
        xds_list = \
            [xds.rename({vis_prod_map[prod]: output_opts.columns[ind]
             for ind, prod in enumerate(output_opts.products)})
             for xds in xds_list]

        output_cols += tuple(output_opts.columns[:n_vis_prod])

    # If the referece xds list exists, we are dealing with BDA data.
    if ref_xds_list:
        xds_list = process_bda_output(xds_list,
                                      ref_xds_list,
                                      output_cols)

    logger.info("Outputs will be written to {}.", ", ".join(output_cols))

    # TODO: Nasty hack due to bug in daskms. Remove ASAP.
    xds_list = [xds.drop_vars(["ANT_NAME", "CHAN_FREQ", "CHAN_WIDTH"],
                              errors='ignore')
                for xds in xds_list]

    write_xds_list = xds_to_table(xds_list, ms_path, columns=output_cols)

    return write_xds_list


def preprocess_xds_list(xds_list, ms_opts):
    """Adds data preprocessing steps - inits flags, weights and fixes bad data.

    Given a list of xarray.DataSet objects, initializes the flag data,
    the weight data and fixes bad data points (NaN, inf, etc). TODO: This
    function can likely be improved/extended.

    Args:
        xds_list: A list of xarray.DataSet objects containing MS data.
        ms_opts: An InputMS config object.

    Returns:
        output_xds_list: A list of xarray.DataSet objects containing MS data
            with preprocessing operations applied.
    """

    output_xds_list = []

    for xds in xds_list:

        # Unpack the data on the xds into variables with understandable names.
        # We create copies of arrays we intend to mutate as otherwise we end
        # up implicitly updating the xds.
        data_col = xds[ms_opts.data_column].data
        flag_col = xds.FLAG.data
        flag_row_col = xds.FLAG_ROW.data

        # Anywhere we have a broken datapoint, zero it. These points will
        # be flagged below. TODO: This can be optimized.

        data_col = da.where(da.isfinite(data_col), data_col, 0)

        weight_col = initialize_weights(xds, data_col, ms_opts.weight_column)

        flag_col = initialise_flags(data_col,
                                    weight_col,
                                    flag_col,
                                    flag_row_col)

        # Anywhere we have a flag, we set the weight to 0.
        weight_col = da.where(flag_col, 0, weight_col)

        # Drop the variables which held the original weights and data -
        # hereafter there are always in DATA and WEIGHT.
        output_xds = xds.drop_vars((ms_opts.data_column,
                                    ms_opts.weight_column),
                                   errors="ignore")

        # Hereafter, DATA is whatever the user specified with data_column.
        # Hereafter, WEIGHT is is whatever the user spcified with
        # weight_column.
        output_xds = output_xds.assign(
            {"DATA": (("row", "chan", "corr"), data_col),
             "WEIGHT": (("row", "chan", "corr"), weight_col),
             "FLAG": (("row", "chan", "corr"), flag_col)})

        output_xds_list.append(output_xds)

    return output_xds_list
