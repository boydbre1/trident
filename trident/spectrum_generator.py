"""
SpectrumGenerator class and member functions.

"""

#-----------------------------------------------------------------------------
# Copyright (c) 2015, Trident Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

import h5py
import numpy as np
import os

from yt.analysis_modules.absorption_spectrum.api import \
    AbsorptionSpectrum
from yt.convenience import \
    load
from yt.funcs import \
    mylog, \
    YTArray

from instrument import \
    Instrument
from ion_balance import \
    add_ion_number_density_field, \
    atomic_mass
from line_database import \
    LineDatabase
from lsf import \
    LSF
from plotting import \
    plot_spectrum

# Valid instruments
valid_instruments = \
    {'COS' :
       Instrument(1150, 1450, dlambda=0.01, lsf_kernel='avg_COS.txt', name='COS'),
     'HIRES' :
       Instrument(1200, 1400, dlambda=0.01, name='HIRES'),
     'UVES' :
       Instrument(1200, 1400, dlambda=0.01, name='UVES'),
     'MODS' :
       Instrument(1200, 1400, dlambda=0.01, name='MODS'),
     'SDSS' :
       Instrument(1200, 1400, dlambda=0.01, name='SDSS')}

class SpectrumGenerator(AbsorptionSpectrum):
    """
    SpectrumGenerator is a subclass of yt's AbsorptionSpectrum class
    with additional functionality like line lists, adding spectral
    templates, and plotting.

    **Parameters**

    lambda_min, lambda_max : int
        The wavelength extrema in angstroms
        Defaults: None

    n_lambda : int
        The number of wavelength bins in the spectrum
        Default: None

    dlambda : float
        The desired wavelength bin width of the spectrum (in angstroms)

    lsf_kernel : string, optional
        The filename for the LSF kernel

    line_database : string, optional
        A text file listing the various lines to insert into the line database.
        The line database provides a list of all possible lines that could
        be added to the spectrum. The file should 4 tab-delimited columns of
        name (e.g. MgII), wavelength in angstroms, gamma of transition, and
        f-value of transition.  See example datasets in trident/data/line_lists
        for examples.
        Default: lines.txt

    ionization_table: hdf5 file, optional
        An HDF5 file used for computing the ionization fraction of the gas
        based on its density, temperature, metallicity, and redshift.
        The format of this file should be... <THIS NEEDS TO BE FINISHED>
    """
    def __init__(self, instrument=None, lambda_min=None, lambda_max=None,
                 n_lambda=None, dlambda=None, lsf_kernel=None,
                 line_database='lines.txt', ionization_table=None):
        if instrument is None and lambda_min is None:
            instrument = 'COS'
            mylog.info("No parameters specified, defaulting to COS instrument.")
        elif instrument is None:
            instrument = Instrument(lambda_min=lambda_min,
                                    lambda_max=lambda_max,
                                    n_lambda=n_lambda,
                                    dlambda=dlambda,
                                    lsf_kernel=lsf_kernel, name="Custom")
        self.set_instrument(instrument)
        mylog.info("Setting instrument to %s" % self.instrument.name)

        AbsorptionSpectrum.__init__(self,
                                    self.instrument.lambda_min,
                                    self.instrument.lambda_max,
                                    self.instrument.n_lambda)

        # instantiate the LineDatabase
        self.line_database = LineDatabase(line_database)

        # store the ionization table in the SpectrumGenerator object
        if ionization_table is not None:
            # figure out where the user-specified files lives
            ionization_table = os.path.join(os.path.dirname(__file__), "..",
                                            "data", "ion_balance", filename)
            if not os.path.isfile(ionization_table):
                ionization_table = filename
            if not os.path.isfile(ionization_table):
                raise RuntimeError("ionization_table %s is not found in local "
                                   "directory or in trident/data/ion_balance "
                                   % (filename.split('/')[-1]))
            self.ionization_table = ionization_table
        else:
            table_dir = os.path.join(os.path.dirname(__file__), '../data/ion_balance')
            filelist = os.listdir(table_dir)
            ion_files = [i for i in filelist if i.endswith('.h5')]
            if 'hm2012_hr.h5' in ion_files: ionization_table = 'hm2012_hr.h5'
            elif 'hm2012_lr.h5' in ion_files: ionization_table = 'hm2012_lr.h5'
            else:
                mylog.info("No ionization file specified, using %s" %ion_files[0])
                ionization_table = ion_files[0]
            self.ionization_table = os.path.join(os.path.dirname(__file__), "..",
                                                 "data", "ion_balance", ionization_table)

    def make_spectrum(self, input_ds, lines=None,
                      output_file=None,
                      use_peculiar_velocity=True, njobs="auto"):
        """
        Make spectrum from ray data using the line list.

        **Parameters**

        input_ds : string or dataset
            path to input ray data or a loaded ray dataset
        lines: list of strings
            List of strings that determine which lines will be added
            to the spectrum.  List can include things like "C", "O VI",
            or "Mg II ####", where #### would be the integer wavelength
            value of the desired line.
        output_file : optional, string
           path for output file.  File formats are chosen based on the
           filename extension.  ".h5" for HDF5, ".fits" for FITS,
           and everything else is ASCII.
           Default: None
        use_peculiar_velocity : optional, bool
           if True, include line of sight velocity for shifting lines.
           Default: True
        njobs : optional, int or "auto"
           the number of process groups into which the loop over
           absorption lines will be divided.  If set to -1, each
           absorption line will be deposited by exactly one processor.
           If njobs is set to a value less than the total number of
           available processors (N), then the deposition of an
           individual line will be parallelized over (N / njobs)
           processors.  If set to "auto", it will first try to
           parallelize over the list of lines and only parallelize
           the line deposition if there are more processors than
           lines.  This is the optimal strategy for parallelizing
           spectrum generation.
           Default: "auto"
        """


        if isinstance(input_ds, str):
            input_ds = load(input_ds)
        ad = input_ds.all_data()

        active_lines = self.line_database.parse_subset(lines)

        # Make sure we've produced all the necessary
        # derived fields if they aren't native to the data
        for line in active_lines:
            try:
                disk_field = ad._determine_fields(line.field)[0]
            except:
                if line.field not in input_ds.derived_field_list:
                    my_ion = \
                      line.field[:line.field.find("number_density")]
                    on_ion = my_ion.split("_")
                    if on_ion[1]:
                        my_lev = int(on_ion[1][1:]) + 1
                    else:
                        my_lev = 1
                add_ion_number_density_field(on_ion[0], my_lev,
                                             self.ionization_table,
                                             input_ds)
            self.add_line(line.identifier, line.field,
                          float(line.wavelength),
                          float(line.f_value),
                          float(line.gamma),
                          atomic_mass[line.element],
                          label_threshold=1e3)

        AbsorptionSpectrum.make_spectrum(self, input_ds,
                                         output_file=output_file,
                                         line_list_file=None,
                                         use_peculiar_velocity=use_peculiar_velocity,
                                         njobs=njobs)

    def _get_qso_spectrum(self, redshift=0.0, filename=None):
        """
        Read in the composite QSO spectrum and return an interpolated version
        to fit the desired wavelength interval and binning.
        """

        if filename is None:
            filename = os.path.join(os.path.dirname(__file__), "..", "data",
                                    "spectral_templates",
                                    "qso_background_COS_HST.txt")

        data = np.loadtxt(filename)
        qso_lambda = YTArray(data[:, 0], 'angstrom')
        qso_lambda += qso_lambda * redshift
        qso_flux = data[:, 1]

        index = np.digitize(self.lambda_bins, qso_lambda)
        np.clip(index, 1, qso_lambda.size - 1, out=index)
        slope = (qso_flux[index] - qso_flux[index - 1]) / \
          (qso_lambda[index] - qso_lambda[index - 1])
        my_flux = slope * (self.lambda_bins - qso_lambda[index]) + qso_flux[index]
        return my_flux

    def _get_milky_way_foreground(self, filename=None):
        """
        Read in the composite QSO spectrum and return an interpolated version
        to fit the desired wavelength interval and binning.
        """

        if filename is None:
            filename = os.path.join(os.path.dirname(__file__), "..", "data",
                                    "spectral_templates",
                                    "mw_foreground_COS.txt")

        data = np.loadtxt(filename)
        MW_lambda = YTArray(data[:, 0], 'angstrom')
        MW_flux = data[:, 1]

        index = np.digitize(self.lambda_bins, MW_lambda)
        np.clip(index, 1, MW_lambda.size - 1, out=index)
        slope = (MW_flux[index] - MW_flux[index - 1]) / \
          (MW_lambda[index] - MW_lambda[index - 1])
        my_flux = slope * (self.lambda_bins - MW_lambda[index]) + MW_flux[index]
        # just set values that go beyond the data to 1
        my_flux[self.lambda_bins > 1799.9444] = 1.0
        return my_flux

    def add_milky_way_foreground(self, flux_field=None,
                                 filename=None):
        """
        Add a Milky Way foreground flux to the spectrum.

        **Parameters**

        flux_field : optional, array
            array of flux values to which the Milky Way foreground is applied.
            Default: None
        filename : string
            filename where the Milky Way foreground values used to modify
            the flux are stored.
            Default: None
        """
        if flux_field is None:
            flux_field = self.flux_field
        MW_spectrum = self._get_milky_way_foreground(filename=filename)
        flux_field *= MW_spectrum

    def add_qso_spectrum(self, flux_field=None,
                         redshift=0.0, filename=None):
        """
        Add a composite QSO spectrum to the spectrum.

        **Parameters**

        flux_field : optional, array
            array of flux values to which the Milky Way foreground is applied.
            Default: None
        redshift: float
            redshift value for defining the rest wavelength of the QSO
            Default: 0.0
        filename : string
            filename where the Milky Way foreground values used to modify
            the flux are stored.
            Default: None
        """
        if flux_field is None:
            flux_field = self.flux_field
        qso_spectrum = self._get_qso_spectrum(redshift=redshift,
                                              filename=filename)
        flux_field *= qso_spectrum

    def add_gaussian_noise(self, snr, n_bins=None, out=None, seed=None):
        """
        Add random gaussian noise to the spectrum.

        **Parameters**

        snr : int
            The desired signal-to-noise ratio for adding the gaussian noise
        n_bins: int
            <I'm not entirely sure what functionality this has>
            Default: None
        out : array
            Array of flux values to which the noise will be added
            <note from devin: should we rename this something more intuitive?>
            Default: None
        seed : optional, int
            Seed for the random number generator.  This should be used to
            ensure than the same noise is adding each time the spectrum is
            regenerated, if desired.
            Default: None

        """
        np.random.seed(seed)
        if n_bins is None:
            n_bins = self.lambda_bins.size
        if out is None:
            out = self.flux_field
        np.add(out, np.random.normal(loc=0.0, scale=1/float(snr), size=n_bins),
               out=out)
        return out

    def apply_lsf(self, function=None, width=None, filename=None):
        """
        Apply the LSF to the flux_field of the spectrum.
        If an instrument already supplies a valid filename and no keywords
        are supplied, it is used by default.  Otherwise, the user can
        specify a filename of a user-defined kernel or a function+width
        for a kernel.  Valid functions are: "boxcar" and "gaussian".

        **Parameters**

        function : string, optional
            desired functional form for the applied LSF kernel.
            Valid options are currently "boxcar" or "gaussian"
            Default: None
        width : int, optional
            width of the desired LSF kernel
            Default: None
        filename : string, optional
            The filename of the user-supplied kernel for applying the LSF
            Default: None
        """
        # if nothing is specified, then use the Instrument-defined kernel
        if function is None and width is None and filename is None:
            if self.instrument.lsf_kernel is None:
                raise RuntimeError("To apply a line spread function, you "
                                   "must specify one or use an instrument "
                                   "where one is defined.")
            else:
                mylog.info("Applying default line spread function for %s." % \
                       self.instrument.name)
                lsf = LSF(filename=self.instrument.lsf_kernel)
        else:
            mylog.info("Applying specified line spread function.")
            lsf = LSF(function=function, width=width, filename=filename)
        self.flux_field = np.convolve(lsf.kernel,self.flux_field,'same')

    def load_spectrum(self, filename=None):
        """
        Load a previously generated spectrum.

        **Parameters**

        filename : string
            The HDF5 file from which the previously generated spectrum
            should be read.  Note: only HDF5 files can currently be reloaded.
            Default: None
        """
        if not filename.endswith(".h5"):
            raise RuntimeError("Only hdf5 format supported for loading spectra.")
        in_file = h5py.File(filename, "r")
        self.lambda_bins = in_file['wavelength'].value
        self.flux_field = in_file['flux'].value
        in_file.close()

    def make_flat_spectrum(self):
        """
        Makes a flat spectrum devoid of any lines.
        """
        self.flux_field = np.ones(self.lambda_bins.size)
        return (self.lambda_bins, self.flux_field)

    def set_instrument(self, instrument):
        """
        Sets the appropriate range of wavelengths and binsize for the
        output spectrum as well as the line spread function.

        set_instrument accepts either the name of a valid instrument or
        a fully specified Instrument object.

        Valid instruments are: %s

        **Parameters**

        instrument : instrument object
            The instrument object that should be used to create the spectrum.
        """ % valid_instruments.keys()

        if isinstance(instrument, str):
            if instrument not in valid_instruments:
                raise RuntimeError("set_instrument accepts only Instrument "
                                   "objects or the names of valid "
                                   "instruments: ", valid_instruments.keys())
            self.instrument = valid_instruments[instrument]
        elif isinstance(instrument, Instrument):
            self.instrument = instrument
        else:
            raise RuntimeError("set_instrument accepts only Instrument "
                               "objects or the names of valid instruments: ",
                               valid_instruments.keys())

    def add_line_to_database(self, element, ion_state, wavelength, gamma,
                             f_value, field=None, identifier=None):
        """
        Adds desired line to the current LineDatabase object.

        **Parameters**

        element : string
            The element of the transition using element's symbol on periodic table

        ion_state : string
            The roman numeral representing the ionic state of the transition

        wavelength : float
            The wavelength of the transition in angstroms

        gamma : float
            The gamma of the transition in Hertz

        f_value: float
            The oscillator strength of the transition

        field : string, optional
            The default yt field name associated with the ion responsible for
            this line
            Default: None

        identifier : string, optional
            An optional identifier for the transition
            Default: None
        """
        self.line_database.add_line(element, ion_state, wavelength,
                                    gamma, f_value, field=field,
                                    identifier=identifier)

    def save_spectrum(self, filename='spectrum.h5', format=None):
        """
        Save the current spectral data to an output file.  Unless specified, 
        the output data format will be determined by the suffix of the filename
        provided ("h5":HDF5, "fits":FITS, all other:ASCII). 

        """
        if format is None:
            if filename.endswith('.h5'):
                self._write_spectrum_hdf5(filename)
            elif filename.endswith('.fits'):
                self._write_spectrum_fits(filename)
            else:
                self._write_spectrum_ascii(filename)
        elif format == 'HDF5':
            self._write_spectrum_hdf5(filename)
        elif format == 'FITS':
            self._write_spectrum_fits(filename)
        elif format == 'ASCII':
            self._write_spectrum_ascii(filename)
        else:
            mylog.warn("Invalid format.  Must be 'HDF5', 'FITS', 'ASCII'. Defaulting to ASCII.")
            self._write_spectrum_ascii(filename)

    def plot_spectrum(self, filename="spectrum.png",
                      lambda_limits=None, flux_limits=None,
                      title=None, label=None,
                      stagger=0.2):
        """
        Plot the spectrum from the SpectrumGenerator class.

        This is a convenience method that wraps the plot_spectrum standalone
        function for use with the data from the SpectrumGenerator itself.
    
        Parameters
    
        filename : string, optional
    
        title : string, optional
            title for plot
    
        label : string or list of strings, optional
            label for each spectrum to be plotted
        """
        plot_spectrum(self.lambda_bins, self.flux_field, filename=filename,
                      lambda_limits=lambda_limits, flux_limits=flux_limits,
                      title=title)
