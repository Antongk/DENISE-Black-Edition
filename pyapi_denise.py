""" Python interface for DENISE-Black-Edition package for seismic wave propagation
and inversion.

by Oleg Ovcharenko, Vladimir Kazei and Daniel Koehn
KAUST, 23955, Thuwal, Saudi Arabia, 2020

oleg.ovcharenko@kaust.edu.sa
vladimir.kazei@kaust.edu.sa
daniel.koehn@ifg.uni-kiel.de

----------
DENISE Black Edition implements 2D time-domain isotropic (visco)elastic finite-difference modeling and full waveform
inversion (FWI) code for P/SV-waves, which Daniel Koehn developed together with André Kurzmann, Denise De Nil and
Thomas Bohlen.

Source code available at:
https://github.com/daniel-koehn/DENISE-Black-Edition

Manual available at:
https://danielkoehnsite.wordpress.com/software/

----------
Quick start:
    1. Navigate to DENISE-Black-Edition installation folder
    2. Run `python denise_api.py --demo` to launch demo

This will create `./outputs/` folder with all parameter files and outputs. Resulting wave fields might be found
in .su format in the `./outputs/su/` subfolder.  Visualize these by Madagascar command
`< ./outputs/su/shot.su sfsegyread su=y endian=n | sfgrey | sfpen`

----------
Example (see full example in the end of the file):
    >>> import denise_api as api
    >>>
    >>> denise = api.Denise('./', verbose=1)
    >>> denise.help()
    >>>
    >>> denise.NPROCX = 5
    >>> denise.NPROCY = 3
    >>> ...
    >>> model = api.Model(vp, vs, rho, dx)
    >>> src = api.Sources(xsrc, ysrc)
    >>> rec = api.Receivers(xrec, yrec)
    >>>
    >>> denise.forward(model, src, rec)
    >>>
    >>> for i in range(3):
            d.add_fwi_stage(fc_low=0.0, fc_high=(i + 1) * 5.0)
    >>>
    >>> denise.fwi(model_init, src, rec)

Check demo.ipynb for a complete example
"""
import argparse
import ast
import os
import re
import time

import numpy as np
import segyio


def natsorted(iterable, key=None, reverse=False):
    """ https://github.com/bdrung/snippets/blob/master/natural_sorted.py """
    prog = re.compile(r"(\d+)")

    def alphanum_key(element):
        return [int(c) if c.isdigit() else c for c in prog.split(key(element)
                                                                 if key else element)]

    return sorted(iterable, key=alphanum_key, reverse=reverse)


def _check_keys(keys):
    keys = keys if keys is not None else []
    keys = [keys] if not isinstance(keys, (list, tuple)) else keys
    return keys


class Denise(object):
    """
    Public methods:
        forward:        starts forward modeling
        fwi:            starts full-waveform inversion
        help:           prints the .inp parameter file
        clean:          hard delete of the save_folder (!!!)
        get_shots:      read shots into a list of numpy arrays
        add_fwi_stage:  append a dict with fwi parameters to self.fwi_stages
        set_model:      extract model dimensions
        set_paths:      build paths if something changed
    """

    def __init__(self, root_denise=None, verbose=1, **kwargs):
        super().__init__(**kwargs)
        self.verbose = verbose

        # WARNING! There is limitation of length for total path of about 72 symbols
        try:
            self._root_denise = root_denise if root_denise else os.environ['DENISE']
        except KeyError:
            # Assume the script is in the installation folder
            self._root_denise = os.getcwd()

        print(f'Init Python 3 API for Denise-Black-Edition.\n'
              f"Check binary in {os.path.join(self._root_denise, 'bin/denise')}")

        self.model = Model()
        self.fwi_stages = []
        self._map_args = dict()
        self._inp_file = None

        # Set attributes from ./par/*.inp file
        self._parse_inp_file()

        self.save_folder = './outputs/'
        self.SEIS_FILE_NAME = 'seis'

        self.DT, self.NT = None, None

        self.set_paths()

    def __repr__(self):
        return f'DENISE-Black-Edition:\n\t{self._root_denise}\nSave folder:\n\t{self.save_folder}\n' \
            f'Run .help() for more details'

    @property
    def _root_su(self):
        """ Path to wave field data """
        return os.path.join(self.save_folder, "su")

    @property
    def _root_model(self):
        """ Path to output models from FWI """
        return os.path.join(self.save_folder, "model")

    @property
    def _root_gradients(self):
        """ Path to gradients from FWI """
        return os.path.join(self.save_folder, "jacobian")

    def _print_1(self, txt, **kwargs):
        """ Conditional print, verbose=1 """
        if self.verbose == 1:
            print(txt, **kwargs)

    def _print_2(self, txt, **kwargs):
        """ Conditional print, verbose=2 """
        if self.verbose == 2:
            print(txt, **kwargs)

    def _dict_to_args(self, d):
        """ Convert dict to class attributes """
        for k, v in d.items():
            setattr(self, k, v)

    def help(self):
        """ Print out parameters file """
        self._write_inp_file(write=False)

    def set_model(self, model):
        """ Extract model dimensions. If model is a tuple of (np.array, float), then wrap them into Model class """
        if isinstance(model, tuple):
            model = Model().from_array(*model)

        self.NY, self.NX = model.vp.shape[:2]
        self.DH = model.dx
        self.model = model

    def _write_model(self):
        """ Create binaries for vp, vs and rho components of velocity model """
        self._print_1(f'Write binaries for\n\t{self.MFILE}.vp\n\t{self.MFILE}.vs\n\t{self.MFILE}.rho')
        _write_binary(self.model.vp, self.MFILE + '.vp')
        _write_binary(self.model.vs, self.MFILE + '.vs')
        _write_binary(self.model.rho, self.MFILE + '.rho')

    def _engine(self, model, src, rec, run_command, disable):
        """ Creates folders, and executes C binaries"""
        self.set_paths(makedirs=True)
        self.set_model(model)

        self.NREC = len(rec.x)
        self.NSRC = len(src.x)

        # Simulation analysis
        self._check_max_frequency()
        self._check_domain_decomp()

        poisson_ratio = _get_poisson_ratio(self.model).mean().mean()
        vs_mean = self.model.vs.mean().mean()

        self._print_1(f'Check elastic ratios:')
        self._print_1(f'\tPoisson ratio:\t\t{poisson_ratio}')
        self._print_1(f'\tShear wave velocity:\t{vs_mean}')
        self._print_1(f'\tRayleigh velocity:\t{vs_mean * (0.862 + 1.14 * poisson_ratio) / (1 + poisson_ratio)}')

        # Compute time step so it is consistent with seismic records
        if self.MODE < 1:
            # If forward modeling was run before, use the same DT
            self._print_1('Compute DT from model given as argument')
            self.DT = self._check_stability(np.max(self.model.vp[np.nonzero(self.model.vp)]),
                                            np.max(self.model.vs[np.nonzero(self.model.vs)]))
            self.NT = int(self.TIME / self.DT)
        elif self.DT is None:
            self._print_1('Compute DT from BOX constraints. VPUPPERLIM, VSUPPERLIM')
            # If FWI is running without forward modeling (e.g. true model is unknown), use velocity box conditions
            self.DT = self._check_stability(self.VPUPPERLIM, self.VSUPPERLIM)

        # Write files
        self._write_model()

        if self.N_STREAMER > 0:
            # Workaround to enable streamer mode. It is deprecated in the original C implementation
            self._print_1('Enable streamer mode!')
            self.READREC = 2
            rec_filename = self.REC_FILE
            for isrc in range(1, src.nsrc + 1):
                _rec = rec
                _rec.x = rec.x  # + (isrc - 1) * self.REC_INCR_X
                _rec.y = rec.y  # + (isrc - 1) * self.REC_INCR_Y
                self.REC_FILE = f'{rec_filename}_shot_{isrc}'
                self._print_1(f'\tsource {isrc}: {self.REC_FILE}')
                self._write_src_rec(src, _rec)
            self.REC_FILE = rec_filename
        else:
            self._write_src_rec(src, rec)
        self._write_inp_file()

        time1 = time.time()
        run_command = run_command if run_command else f"mpirun -np {self.NPROCX * self.NPROCY}"
        self._print_1(f'Start simulation for NREC: {self.NREC}, NSRC: {self.NSRC}, NT: {self.NT}...wait\n')

        if not disable:
            _cmd(f"{run_command} " +
                 ' ' + os.path.join(self._root_denise, "bin/denise ") +
                 ' ' + self.filename +
                 ' ' + self.filename_workflow)

            print(f"\nDone. {time.time() - time1} sec.\n\n"
                  f"Check results in {self.save_folder}su/")

    def forward(self, model, src=None, rec=None, run_command=None, disable=False):
        """ Run forward modeling """
        self.MODE = 0
        self._engine(model, src, rec, run_command, disable)

    def fwi(self, model, src=None, rec=None, run_command=None, disable=False):
        """ Run full-waveform inversion """
        self._print_1(f'Target data: {self.DATA_DIR}')
        self.MODE = 1
        if len(self.fwi_stages) > 0:
            self._print_1(f'Create FWI workflow file in {self.filename_workflow}')
            self._write_denise_workflow_header()
            for i, stage in enumerate(self.fwi_stages):
                self._print_2(f'\twrite FWI stage {i}:\t{stage}\n')
                self._write_denise_workflow(stage)
            self._engine(model, src, rec, run_command, disable)
        else:
            print('WARNING! Denise.fwi_stages is empty! Append api.StageFWI() to it.')

    def _get_filenames(self, dir, keys=None):
        """ List all files from dir which contain keys"""
        keys = _check_keys(keys)
        files = [os.path.join(dir, f) for f in os.listdir(dir)]
        files = [f for f in files if all(True if k in f else False for k in keys)]
        return natsorted(files)

    def get_shots(self, idx=None, keys=None, return_filenames=False):
        """ Reads data from self.savefolder/su/

        Args:
            idx (int, list): shot id, if None, all shots returned
            keys (str, list): substrings to be present in filenames
            return_filenames (bool): return filenames together with data

        Returns:
            List of np.ndarrays, or (list of np.ndarrays, list of strings) if return_filenames enabled
        """
        keys = _check_keys(keys)
        keys.append('.su')

        files = self._get_filenames(self._root_su, keys)
        if idx is not None:
            if not isinstance(idx, list):
                idx = [idx]
            for idx_ in idx:
                files = [f for f in files if f'.shot{idx_}' in f]
        if return_filenames:
            return self._from_su(files), files
        else:
            return self._from_su(files)

    def _read_bins(self, dir, keys=None, return_filenames=False):
        """ Read binaries which contain keys from a dir

        Args:
            dir (str): path to folder with data
            keys (str, list): substrings to be present in filenames
            return_filenames (bool): return filenames together with data

        Returns:
            List of np.ndarrays, or (list of np.ndarrays, list of strings) if return_filenames enabled
        """
        files = self._get_filenames(dir, keys)
        return self._from_bin(files, return_filenames)

    def get_fwi_models(self, keys=None, return_filenames=False):
        """ Read output models from FWI from self.save_folder/model/

        Args:
            keys (str, list): substrings to be present in filenames
            return_filenames (bool): return filenames together with data

        Returns:
            List of np.ndarrays, or (list of np.ndarrays, list of strings) if return_filenames enabled
        """
        keys = _check_keys(keys)
        keys.append('.bin')
        where = self._root_model
        print(f'Read models from {where} with {keys}')
        return self._read_bins(where, keys, return_filenames)

    def get_fwi_gradients(self, keys=None, return_filenames=False):
        """ Read gradients from FWI from self.save_folder/jacobian/

        Args:
            keys (str, list): substrings to be present in filenames
            return_filenames (bool): return filenames together with data

        Returns:
            List of np.ndarrays, or (list of np.ndarrays, list of strings) if return_filenames enabled
        """
        keys = _check_keys(keys)
        return self._read_bins(self._root_gradients, keys, return_filenames)

    def clean(self):
        """ Hard delete of self.save_folder """
        _cmd(f'rm -rf {self.save_folder}')

    def set_paths(self, makedirs=False):
        """ Adds/updates paths to a dict of parameters """

        if makedirs:
            _create_folders(self.save_folder)

        def route(old_path):
            return os.path.join(self.save_folder, old_path)

        s = self.SEIS_FILE_NAME
        p = dict()
        p['DATA_DIR'] = route(f'su/{s}')
        p['filename'] = route(f'{s}.inp')
        p['filename_workflow'] = route(f'{s}_fwi.inp')
        p["MFILE"] = route(f"start/model")
        p["SEIS_FILE_VX"] = route(f"su/{s}_x.su")  # filename for vx component
        p["SEIS_FILE_VY"] = route(f"su/{s}_y.su")  # filename for vy component
        p["SEIS_FILE_CURL"] = route(f"su/{s}_rot.su")  # filename for rot_z component ~ S-wave energy
        p["SEIS_FILE_DIV"] = route(f"su/{s}_div.su")  # filename for div component ~ P-wave energy
        p["SEIS_FILE_P"] = route(f"su/{s}_p.su")
        p["REC_FILE"] = route('receiver/receivers')
        p["SOURCE_FILE"] = route("source/sources.dat")
        p["SIGNAL_FILE"] = route(f"wavelet/wavelet_{s}")
        p["SNAP_FILE"] = route("snap/waveform_forward")
        p["LOG_FILE"] = route(f"log/{s}.log")  # Log file name
        # ----------------------------
        # FWI
        p["JACOBIAN"] = route("jacobian/gradient_Test")  # location and basename of FWI gradients
        p["MISFIT_LOG_FILE"] = route(f"{s}_fwi_log.dat")
        p["INV_MODELFILE"] = route("model/modelTest")  # model location and basename
        p["TRKILL_FILE"] = route(
            "trace_kill/trace_kill.dat")  # Location and name of trace mute file containing muting matrix
        p["PICKS_FILE"] = route("picked_times/picks_")
        p["DATA_DIR_T0"] = route(f"su/CAES_spike_time_0/{s}_CAES")
        p["DFILE"] = route(f"gravity/background_density.rho")

        self._dict_to_args(p)

    def _parse_inp_file(self):
        """ Parse original .ini file and init respective arguments """
        para = {}
        count = 0
        fname = os.path.join(self._root_denise, 'par/', 'DENISE_marm_OBC.inp')
        self._print_1(f'Parse {fname}')
        with open(fname) as fp:
            self._inp_file = fp.readlines()
            for line in self._inp_file:
                count += 1
                line = line.strip()
                # self._print("{}: {}".format(count, line))
                # For not commented lines
                if not line[0] == '#':
                    if '(' in line:
                        # description_(ARG) = VAL
                        arg_name = re.findall(r'\((.*?)\)', line)[-1]
                        arg_val = line.split('=')[-1].strip()
                    elif '=' in line:
                        # ARG = VAL
                        arg_name = line.split('=')[0].strip()
                        arg_val = line.split('=')[-1].strip()
                    else:
                        break

                    # Recognize data type, if fails use string
                    try:
                        # Numeric
                        para[arg_name] = ast.literal_eval(arg_val)
                    except (SyntaxError, ValueError):
                        # String
                        para[arg_name] = arg_val

                    # Store line number from original file
                    self._map_args[arg_name] = count

                    self._print_2(f'\t{arg_name} <-- {arg_val}')

        self._dict_to_args(para)

    def _write_inp_file(self, write=True):
        """ Write parameters file """

        c = self._inp_file
        for attr, line_id in self._map_args.items():
            self._print_2(f'{c[line_id - 1]}', end='')
            self._print_2(f'<-- {getattr(self, attr)}')
            c[line_id - 1] = '='.join(c[line_id - 1].split('=')[:-1]) + '= ' + \
                             str(getattr(self, attr)).replace('(', '').replace(')', '') \
                             + '\n'

        c = ''.join(c)
        if write:
            fp = open(self.filename, mode='w')
            fp.write(c)
            fp.close()
        else:
            print(c)
        return

    def _write_src_rec(self, src_, rec_):
        """ Writes .dat files with source and receiver configurations """
        # -----------------------------------------------------------
        # receiver x-coordinates
        # assemble vectors into an array
        tmp = np.zeros(rec_.x.size, dtype=[('var1', float), ('var2', float)])
        tmp['var1'] = rec_.x
        tmp['var2'] = rec_.y
        # write receiver positions to file
        np.savetxt(self.REC_FILE + '.dat', tmp, fmt='%4.3f %4.3f')
        # -----------------------------------------------------------
        # write source positions and properties to file
        # create and open source file
        fp = open(self.SOURCE_FILE, mode='w')
        # write nshot to file header
        fp.write(str(src_.nshot) + "\n")
        # write source properties to file
        for i in range(0, src_.nshot):
            fp.write('{:4.2f}'.format(src_.x[i]) +
                     "\t" + '{:4.2f}'.format(src_.z[i]) +
                     "\t" + '{:4.2f}'.format(src_.y[i]) +
                     "\t" + '{:1.2f}'.format(src_.td[i]) +
                     "\t" + '{:4.2f}'.format(src_.fc[i]) +
                     "\t" + '{:1.2f}'.format(src_.amp[i]) +
                     "\t" + '{:1.2f}'.format(src_.angle[i]) +
                     "\t" + str(src_.src_type[i]) +
                     "\t" + "\n")

        # Save receiver loacation in .npy
        save_location = self.REC_FILE.split('/')[:-1]
        np.save(f"{'/'.join(save_location)}/rec_x.npy", rec_.x)
        np.save(f"{'/'.join(save_location)}/rec_y.npy", rec_.y)

        # Save source loacation in .npy
        save_location = self.SOURCE_FILE.split('/')[:-1]
        np.save(f"{'/'.join(save_location)}/src_x.npy", rec_.x)
        np.save(f"{'/'.join(save_location)}/src_y.npy", rec_.y)

        fp.close()

    def _check_stability(self, maxvp, maxvs):
        """ Compute time step according to CFL condition

        Args:
            maxvp: maximum velocity of compressional waves
            maxvs: maximum velocity of shear waves

        Returns:
            time step, DT (float)
        """
        self._print_1(f'Check stability:')
        # define FD operator weights for Taylor and Holberg operators:
        # Taylor coefficients
        if (self.max_relative_error == 0):
            hc = np.array([[1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                           [9.0 / 8.0, -1.0 / 24.0, 0.0, 0.0, 0.0, 0.0],
                           [75.0 / 64.0, -25.0 / 384.0, 3.0 / 640.0, 0.0, 0.0, 0.0],
                           [1225.0 / 1024.0, -245.0 / 3072.0, 49.0 / 5120.0, -5.0 / 7168.0, 0.0, 0.0],
                           [19845.0 / 16384.0, -735.0 / 8192.0, 567.0 / 40960.0, -405.0 / 229376.0, 35.0 / 294912.0,
                            0.0],
                           [160083.0 / 131072.0, -12705.0 / 131072.0, 22869.0 / 1310720.0, -5445.0 / 1835008.0,
                            847.0 / 2359296.0, -63.0 / 2883584.0]])

        # Holberg coefficients (E = 0.1%)
        if (self.max_relative_error == 1):
            hc = np.array([[1.001, 0.0, 0.0, 0.0, 0.0, 0.0],
                           [1.1382, -0.046414, 0.0, 0.0, 0.0, 0.0],
                           [1.1965, -0.078804, 0.0081781, 0.0, 0.0, 0.0],
                           [1.2257, -0.099537, 0.018063, -0.0026274, 0.0, 0.0],
                           [1.2415, -0.11231, 0.026191, -0.0064682, 0.001191, 0.0],
                           [1.2508, -0.12034, 0.032131, -0.010142, 0.0029857, -0.00066667]])

        # Holberg coefficients (E = 0.5%)
        if (self.max_relative_error == 2):
            hc = np.array([[1.005, 0.0, 0.0, 0.0, 0.0, 0.0],
                           [1.1534, -0.052806, 0.0, 0.0, 0.0, 0.0],
                           [1.2111, -0.088313, 0.011768, 0.0, 0.0, 0.0],
                           [1.2367, -0.10815, 0.023113, -0.0046905, 0.0, 0.0],
                           [1.2496, -0.11921, 0.03113, -0.0093272, 0.0025161, 0.0],
                           [1.2568, -0.12573, 0.036423, -0.013132, 0.0047484, -0.0015979]])

        # Holberg coefficients (E = 1.0%)
        if (self.max_relative_error == 3):
            hc = np.array([[1.01, 0.0, 0.0, 0.0, 0.0, 0.0],
                           [1.164, -0.057991, 0.0, 0.0, 0.0, 0.0],
                           [1.2192, -0.09407, 0.014608, 0.0, 0.0, 0.0],
                           [1.2422, -0.11269, 0.02614, -0.0064054, 0.0, 0.0],
                           [1.2534, -0.12257, 0.033755, -0.011081, 0.0036784, 0.0],
                           [1.2596, -0.12825, 0.03855, -0.014763, 0.0058619, -0.0024538]])

        # Holberg coefficients (E = 3.0%)
        if (self.max_relative_error == 4):
            hc = np.array([[1.03, 0.0, 0.0, 0.0, 0.0, 0.0],
                           [1.1876, -0.072518, 0.0, 0.0, 0.0, 0.0],
                           [1.2341, -0.10569, 0.022589, 0.0, 0.0, 0.0],
                           [1.2516, -0.12085, 0.032236, -0.011459, 0.0, 0.0],
                           [1.2596, -0.12829, 0.038533, -0.014681, 0.007258, 0.0],
                           [1.264, -0.13239, 0.042217, -0.017803, 0.0081959, -0.0051848]])

        # estimate maximum s-wave velocity != 0 in the model
        self._print_1(f"\tmax Vs: {maxvs}  m/s")

        # estimate maximum p-wave velocity != 0 in the model
        self._print_1(f"\tmax Vp: {maxvp} m/s")

        # estimate maximum seismic velocity in model
        maxvel = maxvp
        if (maxvp < maxvs):
            maxvel = maxvs

        # calculate dt according to CFL criterion
        fdcoeff = (int)(self.FD_ORDER / 2) - 1
        gamma = np.sum(np.abs(hc[fdcoeff, :]))
        dt = self.DH / (np.sqrt(2.) * gamma * maxvel)

        self._print_1("\tAccording to the Courant-Friedrichs-Lewy (CFL) criterion")
        self._print_1("\tthe maximum time step is DT = {:.2e} sec".format(dt))

        # Round the dt toward min
        _dt = 0.99 * dt
        _dt10 = 10 ** (np.log10(_dt) // 1 - 1)
        last2digits = (_dt / _dt10)
        endings = [10, 20, 25, 50, 80]  # possible endings of time step for compatibility with common seismic
        mask = [True if last2digits >= e else False for e in endings]
        last2digits = np.max([e for i, e in enumerate(endings) if mask[i]])
        dt_round = last2digits * _dt10
        self._print_1(f'\tRounded dt = {dt_round}')
        return dt_round

    def _check_max_frequency(self):
        """ Compute maximum frequency of source wavelet based on grid dispersion criterion """
        vp = self.model.vp
        vs = self.model.vs
        # define gridpoints per minimum wavelength for Taylor and Holberg operators
        gridpoints_per_wavelength = np.array([[23.0, 8.0, 6.0, 5.0, 5.0, 4.0],
                                              [49.7, 8.32, 4.77, 3.69, 3.19, 2.91],
                                              [22.2, 5.65, 3.74, 3.11, 2.8, 2.62],
                                              [15.8, 4.8, 3.39, 2.9, 2.65, 2.51],
                                              [9.16, 3.47, 2.91, 2.61, 2.45, 2.36]])

        # estimate minimum s-wave velocity != 0 in the model
        minvs = np.min(vs[np.nonzero(vs)])
        self._print_1('Check max source frequency:')
        self._print_1(f"\tmin Vs: {minvs} m/s")

        # estimate minimum p-wave velocity != 0 in the model
        minvp = np.min(vp[np.nonzero(vp)])
        self._print_1(f"\tmin Vp: {minvp} m/s")

        # estimate minimum seismic velocity in model
        minvel = minvs
        if (minvp < minvs):
            minvel = minvp

        # calculate maximum frequency of the source wavelet based on grid dispersion criterion
        num_grid_points = gridpoints_per_wavelength[self.max_relative_error, (int)(self.FD_ORDER / 2) - 1]
        self._print_1(f"\tNumber of gridpoints per minimum wavelength = {num_grid_points}")
        fmax = minvel / (
                gridpoints_per_wavelength[self.max_relative_error, (int)(self.FD_ORDER / 2) - 1] * self.DH)
        self._print_1(f"\tMaximum source wavelet frequency = {fmax} Hz")

    def _check_domain_decomp(self):
        """ Ensure that NX % NPROCX ==0 and NY % NPROCY == 0 """
        para = self.__dict__

        def check(s):
            decomp = para[f"N{s}"] % para[f"NPROC{s}"]
            self._print_1(f"\tin {s}-direction, N{s} % NPROC{s}, "
                          f"{para[f'N{s}']} % {para[f'NPROC{s}']} = {decomp}")

            assert decomp == 0, f'Error!!! Make sure N{s} % NPROC{s} = 0'

        self._print_1('Check domain decomposition for parallelization:')
        for xy in ['X', 'Y']:
            check(xy)

    def _from_su(self, files):
        """ Read .su files into a list of np.ndarrays """
        if not isinstance(files, (list, tuple)):
            files = [files]
        outs = []
        for file in files:
            with segyio.su.open(file, endian='little', ignore_geometry=True) as f:
                outs.append(np.array([np.copy(tr) for tr in f.trace]))
                self._print_1(f'< {file} > np.array({outs[-1].shape})')
        return outs

    def _from_bin(self, files, return_filenames=False):
        """ Read .bin files into a list of np.ndarrays """
        if not isinstance(files, (list, tuple)):
            files = [files]
        outs = []
        fnames = []
        for file in files:
            self._print_1(f'< {file} > np.array({self.NX, self.NY})')
            f = open(file)
            data_type = np.dtype('float32').newbyteorder('<')
            vs = np.fromfile(f, dtype=data_type)
            try:
                vs = vs.reshape(self.NX, self.NY)
                vs = np.transpose(vs)
                vs = np.flipud(vs)
                outs.append(vs)
                fnames.append(file)
            except ValueError:
                self._print_1(f'\tFailed to reshape {vs.shape} to ({self.NX, self.NY})')
        if return_filenames:
            return outs, fnames
        else:
            return outs

    def _write_denise_workflow_header(self):
        """ Create FWI workflow file and write header into it"""
        fp = open(self.filename_workflow, mode='w')
        fp.write(
            "PRO \t TIME_FILT \t FC_low \t FC_high \t ORDER \t TIME_WIN \t"
            " GAMMA \t TWIN- \t TWIN+ \t INV_VP_ITER \t INV_VS_ITER \t"
            " INV_RHO_ITER \t INV_QS_ITER \t SPATFILTER \t WD_DAMP \t WD_DAMP1"
            " \t EPRECOND \t LNORM \t STF_INV \t OFFSETC_STF \t EPS_STF \t NORMALIZE"
            " \t OFFSET_MUTE \t OFFSETC \t SCALERHO \t SCALEQS \t ENV \t GAMMA_GRAV \t N_ORDER \n")
        fp.close()

    def _write_denise_workflow(self, stage):
        """ Write FWI stage into workflow file

        Args:
            stage (dict): parameters for one stage of full-waveform inversion
        """
        fp = open(self.filename_workflow, mode='a')
        fp.write(str(stage["PRO"]) + "\t" + str(stage["TIME_FILT"]) + "\t" + str(stage["FC_LOW"]) + "\t" + str(
            stage["FC_HIGH"]) + "\t" + str(stage["ORDER"]) + "\t" + str(stage["TIME_WIN"]) + "\t" + str(
            stage["GAMMA"]) + "\t" + str(stage["TWIN-"]) + "\t" + str(stage["TWIN+"]) + "\t" + str(
            stage["INV_VP_ITER"]) + "\t" + str(stage["INV_VS_ITER"]) + "\t" + str(stage["INV_RHO_ITER"]) + "\t" + str(
            stage["INV_QS_ITER"]) + "\t" + str(stage["SPATFILTER"]) + "\t" + str(stage["WD_DAMP"]) + "\t" + str(
            stage["WD_DAMP1"]) + "\t" + str(stage["EPRECOND"]) + "\t" + str(stage["LNORM"]) + "\t" + str(
            stage["STF"]) + "\t" + str(stage["OFFSETC_STF"]) + "\t" + str(stage["EPS_STF"]) + "\t" + "0" + "\t" + str(
            stage["OFFSET_MUTE"]) + "\t" + str(stage["OFFSETC"]) + "\t" + str(stage["SCALERHO"]) + "\t" + str(
            stage["SCALEQS"]) + "\t" + str(stage["ENV"]) + "\t" + "0" + "\t" + str(stage["N_ORDER"]) + "\n")
        fp.close()

    def add_fwi_stage(self, pro=0.01, time_filt=1, fc_low=0.0, fc_high=5.0, order=6, time_win=0, gamma=20,
                      twin_minus=0., twin_plus=0., inv_vp_iter=0, inv_vs_iter=0, inv_rho_iter=0, inv_qs_iter=0,
                      spatfilter=0, wd_damp=0.5, wd_damp1=0.5, e_precond=3, lnorm=2, stf=0, offsetc_stf=-4.,
                      eps_stf=1e-1, normalize=0, offset_mute=0, offsetc=10, scale_rho=0.5, scale_qs=1.0, env=1,
                      n_order=0):
        """ Appends a dict with FWI stage parameters to self.fwi_stages

        Args:
            pro (float): Termination criterion
            time_filt(int): Frequency filtering
                TIME_FILT = 0 (apply no frequency filter to field data and source wavelet)
                TIME_FILT = 1 (apply low-pass filter to field data and source wavelet), default
                TIME_FILT = 2 (apply band-pass filter to field data and source wavelet)
            fc_low (float): low-pass corner frequencies of Butterworth filter
            fc_high (float): high-pass corner frequencies of Butterworth filter
            order(int): order of Butterworth filter
            time_win(int): Time windowing
            gamma (float):
            twin_minus (float):
            twin_plus (float):
            inv_vp_iter (int): start FWI for VP from iteration number
            inv_vs_iter (int): start FWI for VS from iteration number
            inv_rho_iter (int): start FWI for RHO from iteration number
            inv_qs_iter (int): start FWI for QS from iteration number
            spatfilter (int): Apply spatial Gaussian filter to gradients
                SPATFILTER = 0 (apply no filter, default)
                SPATFILTER = 4 (Anisotropic Gaussian filter with half-width adapted to the local wavelength)
            # If Gaussian filter (SPATFILTER=4)
            wd_damp (float):  fraction of the local wavelength in x-direction used to define the half-width of the Gaussian filter
            wd_damp1 (float): fraction of the local wavelength in y-direction used to define the half-width of the Gaussian filter
            e_precond (int): Preconditioning of the gradient directions
                EPRECOND = 0 - no preconditioning
                EPRECOND = 1 - approximation of the Pseudo-Hessian (Shin et al. 2001)
                EPRECOND = 3 - Hessian approximation according to Plessix & Mulder (2004), default
            lnorm (int): Define objective function
                LNORM = 2 - L2 norm, default
                LNORM = 5 - global correlation norm (Choi & Alkhalifah 2012)
                LNORM = 6 - envelope objective functions after Chi, Dong and Liu (2014) - EXPERIMENTAL
                LNORM = 7 - NIM objective function after Chauris et al. (2012) and Tejero et al. (2015) - EXPERIMENTAL
            stf (int): Source wavelet inversion
                STF = 0 - no source wavelet inversion, default
                STF = 1 - estimate source wavelet by stabilized Wiener Deconvolution
            offsetc_stf (float): If OFFSETC_STF > 0, limit source wavelet inversion to maximum offsets OFFSETC_STF
            eps_stf (float): Source wavelet inversion stabilization term to avoid division by zero in Wiener Deco
            normalize (int):
            offset_mute (int): Apply Offset mute to field and modelled seismograms
                OFFSET_MUTE = 0 - no offset mute, default
                OFFSET_MUTE = 1 - mute far-offset data for offset >= OFFSETC
                OFFSET_MUTE = 1 - mute near-offset data for offset <= OFFSETC
            offsetc (float):
            scale_rho (float): Scale density update during multiparameter FWI by factor
            scale_qs (float): Scale Qs update during multiparameter FWI by factor
            env (int): If LNORM = 6, define type of envelope objective function (EXPERIMENTAL)
                ENV = 1 - L2 envelope objective function
                ENV = 2 - Log L2 envelope objective function
            n_order (int): Integrate synthetic and modelled data NORDER times (EXPERIMENTAL)
        """
        para = dict()
        # Termination criterion
        para["PRO"] = pro

        # Frequency filtering
        # TIME_FILT = 0 (apply no frequency filter to field data and source wavelet)
        # TIME_FILT = 1 (apply low-pass filter to field data and source wavelet)
        # TIME_FILT = 2 (apply band-pass filter to field data and source wavelet)
        para["TIME_FILT"] = time_filt

        # Low- (FC_LOW) and high-pass (FC_HIGH) corner frequencies of Butterwortfilter
        # of order ORDER
        para["FC_LOW"] = fc_low
        para["FC_HIGH"] = fc_high
        para["ORDER"] = order

        # Time windowing
        para["TIME_WIN"] = time_win
        para["GAMMA"] = gamma
        para["TWIN-"] = twin_minus
        para["TWIN+"] = twin_plus

        # Starting FWI of parameter class Vp, Vs, rho, Qs from iteration number
        # INV_VP_ITER, INV_VS_ITER, INV_RHO_ITER, INV_QS_ITER
        para["INV_VP_ITER"] = inv_vp_iter
        para["INV_VS_ITER"] = inv_vs_iter
        para["INV_RHO_ITER"] = inv_rho_iter
        para["INV_QS_ITER"] = inv_qs_iter

        # Apply spatial Gaussian filter to gradients
        # SPATFILTER = 0 (apply no filter)
        # SPATFILTER = 4 (Anisotropic Gaussian filter with half-width adapted to the local wavelength)
        para["SPATFILTER"] = spatfilter

        # If Gaussian filter (SPATFILTER=4), define the fraction of the local wavelength in ...
        # x-direction WD_DAMP and y-direction WD_DAMP1 used to define the half-width of the
        # Gaussian filter
        para["WD_DAMP"] = wd_damp
        para["WD_DAMP1"] = wd_damp1

        # Preconditioning of the gradient directions
        # EPRECOND = 0 - no preconditioning
        # EPRECOND = 1 - approximation of the Pseudo-Hessian (Shin et al. 2001)
        # EPRECOND = 3 - Hessian approximation according to Plessix & Mulder (2004)
        para["EPRECOND"] = e_precond

        # Define objective function
        # LNORM = 2 - L2 norm
        # LNORM = 5 - global correlation norm (Choi & Alkhalifah 2012)
        # LNORM = 6 - envelope objective functions after Chi, Dong and Liu (2014) - EXPERIMENTAL
        # LNORM = 7 - NIM objective function after Chauris et al. (2012) and Tejero et al. (2015) - EXPERIMENTAL
        para["LNORM"] = lnorm

        # Source wavelet inversion
        # STF = 0 - no source wavelet inversion
        # STF = 1 - estimate source wavelet by stabilized Wiener Deconvolution
        para["STF"] = stf

        # If OFFSETC_STF > 0, limit source wavelet inversion to maximum offsets OFFSETC_STF
        para["OFFSETC_STF"] = offsetc_stf

        # Source wavelet inversion stabilization term to avoid division by zero in Wiener Deco
        para["EPS_STF"] = eps_stf

        # Disabled?
        para["NORMALIZE"] = normalize

        # Apply Offset mute to field and modelled seismograms
        # OFFSET_MUTE = 0 - no offset mute
        # OFFSET_MUTE = 1 - mute far-offset data for offset >= OFFSETC
        # OFFSET_MUTE = 1 - mute near-offset data for offset <= OFFSETC
        para["OFFSET_MUTE"] = offset_mute
        para["OFFSETC"] = offsetc

        # Scale density and Qs updates during multiparameter FWI by factors
        # SCALERHO and SCALEQS, respectively
        para["SCALERHO"] = scale_rho
        para["SCALEQS"] = scale_qs

        # If LNORM = 6, define type of envelope objective function (EXPERIMENTAL)
        # ENV = 1 - L2 envelope objective function
        # ENV = 2 - Log L2 envelope objective function
        para["ENV"] = env

        # Integrate synthetic and modelled data NORDER times (EXPERIMENTAL)
        para["N_ORDER"] = n_order

        self.fwi_stages.append(para)


# =======================================================================
# Supplementary code starts here

def _get_poisson_ratio(m):
    return 0.5 * (m.vp ** 2 - 2 * m.vs ** 2) / (m.vp ** 2 - m.vs ** 2)


def _create_folders(save_path):
    """ Creates a set of subfolders in save_path """
    _folders = ['start', 'su', 'receiver', 'source', 'snap', 'log', 'wavelet',
                'jacobian', 'model', 'gravity', 'picked_times', 'trace_kill']

    for _f in _folders:
        os.makedirs(os.path.join(save_path, f'{_f}/'), exist_ok=True)


def _write_binary(data, name):
    """ Writes a binary file accounting for indexing """
    f = open(name, mode='wb')
    data_type = np.dtype('float32').newbyteorder('<')
    vp1 = np.array(data, dtype=data_type)
    vp1 = np.rot90(vp1, 3)
    vp1.tofile(f)
    f.close()


def _cmd(command):
    """ Executes a shell command """
    print(command)
    os.system(command)


class _Template(object):
    """ Template for Model, Sources, Receivers classes"""

    def __init__(self, **kwargs):
        pass

    def __repr__(self):
        msg = []
        for k, v in self.__dict__.items():
            if not k.startswith('_'):
                try:
                    if isinstance(v, np.ndarray):
                        msg.append('{}\t{}:\tmin: {}\tmax: {}'.format(k, v.shape, v.min(), v.max()))
                    else:
                        msg.append('{}\t{}:\tmin: {}\tmax: {}'.format(k, v.shape(), v.min(), v.max()))
                except:
                    msg.append('{}:\t{}'.format(k, v))
        return '\n'.join(msg)


class Sources(_Template):
    def __init__(self, x, y, **kwargs):
        """ Wrapper for source locations and source parameters. Axes origin is in top left corner

        Args:
            x (list): locations along OX (horizontal) axis, in meters
            y (list): locations along OY (vertical) axis, in meters
        """
        super().__init__(**kwargs)
        self.x = x
        self.y = y
        self._ones = np.ones_like(x)
        self.z = 0.0 * self._ones
        self._set_default_source()

    def _set_default_source(self):
        self.nshot = len(self.y)  # number of source positions
        self.td = 0.0 * self._ones  # time delay of source wavelet [s]
        self.fc = 8.0 * self._ones  # center frequency of pre-defined source wavelet [Hz]
        # you can also use the maximum frequency computed from the grid dispersion
        # criterion in section 3. based on spatial discretization and FD operator
        # fc = (freqmax / 2.) * (self.x / self.x)
        self.amp = 1.0 * self._ones  # amplitude of source wavelet [m]
        self.angle = 0.0 * self._ones  # angle of rotated source [°]
        self.QUELLTYPB = 1
        self.src_type = self.QUELLTYPB * self._ones

    @property
    def nsrc(self):
        return len(self.x)

    def __len__(self):
        return self.nsrc


class Receivers(_Template):
    def __init__(self, x, y, **kwargs):
        """ Wrapper for receiver locations. Axes origin is in top left corner

        Args:
            x (list): locations along OX (horizontal) axis, in meters
            y (list): locations along OY (vertical) axis, in meters
        """
        super().__init__(**kwargs)
        self.x, self.y = x, y

    @property
    def nrec(self):
        return len(self.x)

    def __len__(self):
        return self.nrec


class Model(_Template):
    def __init__(self, vp=None, vs=None, rho=None, dx=None, **kwargs):
        """ Wrapper for elastic velocity model.

        Args:
            vp (np.ndarray): a model for compressional waves (nz, nx) [m/s]
            vs (np.ndarray): a model for shear waves (nz, nx) [m/s]
            rho (np.ndarray): a model for density (nz, nx) [g/cm3]
            dx (float): grid spacing, equal in both directions
        """
        super().__init__(**kwargs)
        self.vp, self.vs, self.rho = vp, vs, rho
        self.dx = dx

    def __repr__(self):
        msg = []
        msg.append('vp:\t{}, {:.4f}, {:.4f} m/s\n'.format(self.vp.shape, self.vp.min(), self.vp.max()))
        msg.append('vs:\t{}, {:.4f}, {:.4f} m/s\n'.format(self.vs.shape, self.vs.min(), self.vs.max()))
        msg.append('rho:\t{}, {:.4f}, {:.4f} g/cm3\n'.format(self.rho.shape, self.rho.min(), self.rho.max()))
        msg.append('dx:\t{:.4f}'.format(self.dx))
        msg.append('Size:\n\tOX:\tmin {:.4f}\tmax {:.4f} m'.format(self.xmin, self.xmax))
        msg.append('Size:\n\tOZ:\tmin {:.4f}\tmax {:.4f} m'.format(self.zmin, self.zmax))
        return '\n'.join(msg)

    @property
    def xmax(self):
        return (self.nx - 1) * self.dx

    @property
    def zmax(self):
        return (self.nz - 1) * self.dx

    @property
    def xmin(self):
        return 0.

    @property
    def zmin(self):
        return 0.

    @property
    def nx(self):
        return self.vp.shape[1]

    @property
    def nz(self):
        return self.vp.shape[0]

    def from_array(self, x, dx=1.):
        """ Init Model() from an np.ndarray of (nz, nx, 3), where last dims is populated with the following
            0 - Vp [m/s]
            1 - Vs [m/s]
            2 - Rho [g/cm3]

        Args:
            x (np.ndarray): combined velocity model (nz, nx, 3)
            dx (float): grid spacing, equal in both directions
        """
        # ATTENTION this flip is to match data arrangement in the original C code
        x = np.flip(x, 0)
        self.vp = x[..., 0]
        self.vs = x[..., 1]
        self.rho = x[..., 2]
        self.dx = dx if isinstance(dx, (int, float)) else dx[0]
        print(
            f'vp {self.vp.min(), self.vp.max()} vs {self.vs.min(), self.vs.max()} rho {self.rho.min(), self.rho.max()}')
        return self

    def from_npy(self, fname, dx=1.):
        """ Read .npy file and loads a velocity model from it

        Args:
            fname (str): path to .npy file with a combined velocity model (nz, nx, 3)
            dx (float): grid spacing, equal in both directions
        """
        self.from_array(np.load(fname), dx)
        return self


if __name__ == "__main__":
    # Make sure to complete ONE of the following:
    # 1. Place denise_api.py in the same folder with DENISE-Black-Edition installation
    # 2. Specify the root manually as denise = Denise(root_denise='./DENISE-Black-Edition)
    # 3. DENISE environmental variable points to the root of DENISE-Black-Edition installation

    d = Denise('./', verbose=1)

    print(f'\n-----------------\n'
          f'Run DEMO:\n\tpython denise_api.py --demo\n'
          f'\nand check results in ./outputs/')

    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='run demo')
    runtime, unknown = parser.parse_known_args()

    if runtime.demo:
        # Print out all configurable parameters
        # d.help()

        # Amend default parameters
        d.PHYSICS = 1  # Elastic wave prop, 2 for acoustic
        d.NPROCX = 5  # 5 - default
        d.NPROCY = 3  # 3 - default

        # Velocity model, ny (depth) x nx (width)
        ny, nx = 174, 500
        dx = 20  # grid spacing, [m]
        vp = 3500. * np.ones((ny, nx))  # compressional velocity, [m/s]
        vs = vp.copy() / 1.7  # share velocity, [m/s]
        rho = 1800.0 * np.ones_like(vp)  # density, [kg/m3]
        model = Model(vp, vs, rho, dx)

        # Receivers
        drec = 20.
        depth_rec = 460.  # receiver depth [m]
        xrec1 = 800.  # 1st receiver position [m]
        xrec2 = 8780.  # last receiver position [m]
        xrec = np.arange(xrec1, xrec2 + dx, drec)
        yrec = depth_rec * np.ones_like(xrec)
        # Sources
        dsrc = 960  # source spacing [m]
        depth_src = 40.  # source depth [m]
        xsrc1 = 800.  # 1st source position [m]
        xsrc2 = 8780.  # last source position [m]
        xsrc = np.arange(xsrc1, xsrc2 + dx, dsrc)
        ysrc = depth_src * np.ones_like(xsrc)

        rec = Receivers(xrec, yrec)
        src = Sources(xsrc, ysrc)

        # -------------------------------------
        # Forward modeling. Saves data in d.save_folder/su/
        d.forward(model, src, rec)

        # -------------------------------------
        # Full-waveform inversion stages
        for freq in [2, 5, 10, 20]:
            d.add_fwi_stage(fc_low=0.0, fc_high=freq)

        # Run inversion. Saves inverted models for every stage in d.save_folder/model/
        d.fwi(model, src, rec)
