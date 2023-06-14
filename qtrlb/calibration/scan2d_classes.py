import os
import numpy as np
import matplotlib.pyplot as plt
from lmfit import Model
from matplotlib.colors import LinearSegmentedColormap as LSC
from matplotlib.offsetbox import AnchoredText
import qtrlb.utils.units as u
from qtrlb.config.config import MetaManager
from qtrlb.calibration.calibration import Scan2D
from qtrlb.calibration.scan_classes import RabiScan, LevelScan
from qtrlb.processing.fitting import QuadModel
from qtrlb.processing.processing import rotate_IQ, gmm_fit, gmm_predict, normalize_population, \
                                        get_readout_fidelity




class ChevronScan(Scan2D, RabiScan):
    def __init__(self,
                 cfg: MetaManager, 
                 drive_qubits: str | list[str],
                 readout_resonators: str | list[str],
                 length_start: float,
                 length_stop: float,
                 length_points: int,
                 detuning_start: float, 
                 detuning_stop: float, 
                 detuning_points: int, 
                 subspace: str | list[str] = None,
                 main_tones: str | list[str] = None,
                 pregate: dict[str: list[str]] = None,
                 postgate: dict[str: list[str]] = None,
                 n_seqloops: int = 10,
                 level_to_fit: int | list[int] = None,
                 fitmodel: Model = None,  # Don't fit by default.
                 init_waveform_idx: int = 101):
        
        super().__init__(cfg=cfg, 
                         drive_qubits=drive_qubits,
                         readout_resonators=readout_resonators,
                         scan_name='Chevron',
                         x_plot_label='Pulse Length',
                         x_plot_unit='ns',
                         x_start=length_start,
                         x_stop=length_stop,
                         x_points=length_points,
                         y_plot_label='Frequency', 
                         y_plot_unit='MHz', 
                         y_start=detuning_start, 
                         y_stop=detuning_stop, 
                         y_points=detuning_points, 
                         subspace=subspace,
                         main_tones=main_tones,
                         pregate=pregate,
                         postgate=postgate,
                         n_seqloops=n_seqloops,
                         level_to_fit=level_to_fit,
                         fitmodel=fitmodel)
        
        self.init_waveform_index = init_waveform_idx
        
        
    def add_yinit(self):
        """
        Here R6 is the detuning value, which need to be transformed into frequency of sequencer.
        """
        super().add_yinit()
        
        for tone in self.main_tones:
            ssb_freq_start = self.y_start + self.cfg[f'variables.{tone}/mod_freq']
            ssb_freq_start_4 = self.frequency_translator(ssb_freq_start)
            
            yinit = f"""
                    move             {ssb_freq_start_4},R6
            """
            self.sequences[tone]['program'] += yinit


    def add_main(self):
        """
        Qblox doesn't accept zero length waveform, so we use Label 'end_main' here.
        There is 4ns delay after each Rabi pulse before postgate/readout.
        It's because the third index of 'play' instruction cannot be register.
        So we cannot set it as a variable, and wait will be separated.
        """
        super().add_main(freq='R6')


    def add_yvalue(self):
        ssb_freq_step_4 = self.frequency_translator(self.y_step)
        for tone in self.main_tones:  self.sequences[tone]['program'] += f"""
                    add              R6,{ssb_freq_step_4},R6
        """
            

    def fit_data(self, x=None, **fitting_kwargs):
        super().fit_data(x=x, y=self.y_values, **fitting_kwargs)
        
        
class ReadoutTemplateScan(Scan2D, LevelScan):
    """ Sweep a readout parameter (freq/amp/length) with different qubit state.
        Calculate readout fidelity for each parameter value and show the spectrum for all qubit state.
        Require calibrated PI gate when scaning more than ground level.
        This Scan has no __init__ and suppose to be a template inherited by child scan \
        and not to be called directly.
        Since the data in 'to_fit' is readout fidelity, we should always use level_to_fit at the lowest \
        readout level so that the index is always 0.
        
        Note from Zihao(03/16/2023):
        I'm sorry this code is so long. This type of scans don't fit well into our calibration framework, \
        and we have to rebuild some wheels. Although it's possible to leave an interface in some \
        parent method, that will make things too thick, too ugly and hard to read.
    """
    def check_attribute(self):
        super().check_attribute()
        assert not self.classification_enable, 'Please turn off classification.'
        
        
    def process_data(self, compensate_ED: bool = False):
        """
        Here we override the parent method since the processing for this Scan has no similarity to \
        other Scan. Code is similar to CalibrateClassification.fit_data()
        """
        shape = (2, self.n_reps, self.y_points, self.x_points)
        electrical_delay = self.cfg['variables.common/electrical_delay'] if compensate_ED else 0
        phase_offset = np.exp(1j * 2 * np.pi * self.y_values * electrical_delay)
        
        for r, data_dict in self.measurement.items():
            # First get averaged complex IQ vector for each y_value to plot spectrum.
            data_dict['Reshaped_readout'] = np.array(data_dict['Heterodyned_readout']).reshape(shape)
            data_dict['IQrotated_readout'] = rotate_IQ(data_dict['Reshaped_readout'], 
                                                       angle=self.cfg[f'process.{r}/IQ_rotation_angle'])
            data_dict['IQaveraged_readout'] = np.mean(data_dict['IQrotated_readout'], axis=1)
            data_dict['IQcomplex_readout'] = (data_dict['IQaveraged_readout'][0]
                                             + 1j * data_dict['IQaveraged_readout'][1])
            data_dict['IQEDcompensated_readout'] = (data_dict['IQcomplex_readout'].T * phase_offset).T
            
            # Then loop all y_values and use GMM model to fit them and get confusion matrix.
            data_dict['GMMfitted'] = {}
            data_dict['to_fit'] = []
            
            for y in range(self.y_points):
                sub_dict = {}
                means = np.zeros((self.x_points, 2))
                covariances = np.zeros(self.x_points)
                
                for x in range(self.x_points):
                    data = data_dict['IQrotated_readout'][..., y, x]
                    mean, covariance = gmm_fit(data, n_components=1)
                    means[x] = mean[0]
                    covariances[x] = covariance[0]
                    # Because the default form is one more layer nested.
                    
                sub_dict['means'] = means
                sub_dict['covariances'] = covariances
                sub_dict['GMMpredicted'] = gmm_predict(data_dict['IQrotated_readout'][..., y, :], 
                                                       means=means, covariances=covariances)
                sub_dict['confusionmatrix'] = normalize_population(sub_dict['GMMpredicted'], 
                                                                   n_levels=self.x_points)
                sub_dict['ReadoutFidelity'] = get_readout_fidelity(sub_dict['confusionmatrix'])
                
                data_dict['GMMfitted'][f'{y}'] = sub_dict
                data_dict['to_fit'].append(sub_dict['ReadoutFidelity'])
                
            data_dict['to_fit'] = np.array([data_dict['to_fit']])
            # Do not delete this nested structure.
            
            
    def fit_data(self):
        super().fit_data(x=self.y_values)  # We're actually using 1D fitting Scan.fit_data().

        
    def plot(self):
        self.plot_main()
        self.plot_spectrum()
        if self.cfg['variables.common/plot_IQ']: self.plot_IQ()
        

    def plot_main(self, text_loc: str = 'lower right'):
        """
        Plot readout fidelity along with fitting result as function of y_values.
        Code is similar to Scan.plot_main()
        """
        self.figures = {}

        for r in self.readout_resonators:
            title = f'{self.datetime_stamp}, {self.scan_name}, {r}'
            xlabel = self.y_plot_label + f'[{self.y_plot_unit}]'
            ylabel = 'Readout Fidelity [a.u.]'
            
            fig, ax = plt.subplots(1, 1, dpi=150)
            ax.plot(self.y_values / self.y_unit_value, self.measurement[r]['to_fit'][0], 'k.')
            ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
            
            if self.fit_result[r] is not None: 
                # Raise resolution of fit result for smooth plot.
                x = np.linspace(self.y_start, self.y_stop, self.y_points * 3)  
                y = self.fit_result[r].eval(x=x)
                ax.plot(x / self.y_unit_value, y, 'm-')
                
                # AnchoredText stolen from Ray's code.
                fit_text = '\n'.join([f'{v.name} = {v.value:0.5g}' for v in self.fit_result[r].params.values()])
                anchored_text = AnchoredText(fit_text, loc=text_loc, prop={'color':'m'})
                ax.add_artist(anchored_text)

            fig.savefig(os.path.join(self.data_path, f'{r}.png'))
            self.figures[r] = fig

            
    def plot_spectrum(self):
        """
        Plot the phase and Log-magnitude of the IQ data as y_values for all levels.
        For readout amplitude and length, it may not be approprite to call it spectrum.
        But, you get what I mean here :)
        """
        for r in self.readout_resonators:
            data = self.measurement[r]['IQEDcompensated_readout']
            
            title = f'{self.datetime_stamp}, {self.scan_name}, {r}'
            xlabel = self.y_plot_label + f'[{self.y_plot_unit}]'
            ylabel = ['IQ-phase [rad]', 'IQ-LogMag [a.u.]']
            
            fig, ax = plt.subplots(2, 1, figsize=(8,8), dpi=150)
            ax[0].set(xlabel=xlabel, ylabel=ylabel[0], title=title)
            ax[1].set(xlabel=xlabel, ylabel=ylabel[1])
            
            # The level might not always start from 0.
            for i, level in enumerate(self.x_values):
                ax[0].plot(self.y_values / self.y_unit_value, np.angle(data[:, i]), label=f'|{level}>')
                ax[1].plot(self.y_values / self.y_unit_value, np.absolute(data[:, i]), label=f'|{level}>')

            ax[0].legend()
            ax[1].legend()
            fig.savefig(os.path.join(self.data_path, f'{r}_spectrum.png'))
            
            
    def plot_IQ(self):
        """
        Plot IQ data for all y_values, each y_value will have a plot with all levels.
        Code is similar to Scan.plot_IQ()
        """
        for r in self.readout_resonators:
            Is, Qs = self.measurement[r]['IQrotated_readout']
            left, right = (np.min(Is), np.max(Is))
            bottom, top = (np.min(Qs), np.max(Qs))
            
            for y in range(self.y_points):                
                fig, ax = plt.subplots(1, self.x_points, figsize=(6 * self.x_points, 6), dpi=150)
                
                for x in range(self.x_points):
                    I = self.measurement[r]['IQrotated_readout'][0,:,y,x]
                    Q = self.measurement[r]['IQrotated_readout'][1,:,y,x]
                    c = self.measurement[r]['GMMfitted'][f'{y}']['GMMpredicted'][:,x]
                    cmap = LSC.from_list(None, plt.cm.tab10(self.cfg[f'variables.{r}/readout_levels']), 12)
                    
                    ax[x].scatter(I, Q, c=c, cmap=cmap, alpha=0.2)
                    ax[x].axvline(color='k', ls='dashed')    
                    ax[x].axhline(color='k', ls='dashed')
                    ax[x].set(xlabel='I', ylabel='Q', title=fr'${{\left|{self.x_values[x]}\right\rangle}}$', 
                              aspect='equal', xlim=(left, right), ylim=(bottom, top))
                    
                fig.savefig(os.path.join(self.data_path, f'{r}_IQplots', f'{y}.png'))
                plt.close(fig)        
        
        
class ReadoutFrequencyScan(ReadoutTemplateScan):
    def __init__(self,
                 cfg: MetaManager, 
                 drive_qubits: str | list[str],
                 readout_resonators: str | list[str],
                 level_start: int,
                 level_stop: int,
                 detuning_start: float, 
                 detuning_stop: float, 
                 detuning_points: int, 
                 pregate: dict[str: list[str]] = None,
                 postgate: dict[str: list[str]] = None,
                 n_seqloops: int = 10,
                 level_to_fit: int | list[int] = None,
                 fitmodel: Model = QuadModel):
        
        super().__init__(cfg=cfg, 
                         drive_qubits=drive_qubits,
                         readout_resonators=readout_resonators,
                         scan_name='ReadoutFrequency',
                         x_plot_label='Level',
                         x_plot_unit='arb',
                         x_start=level_start,
                         x_stop=level_stop,
                         x_points=level_stop-level_start+1,
                         y_plot_label='Readout Frequency', 
                         y_plot_unit='kHz', 
                         y_start=detuning_start, 
                         y_stop=detuning_stop, 
                         y_points=detuning_points, 
                         pregate=pregate,
                         postgate=postgate,
                         n_seqloops=n_seqloops,
                         level_to_fit=level_to_fit,
                         fitmodel=fitmodel)  
        

    def add_yinit(self):
        super().add_yinit()
        
        for r in self.readout_resonators:
            ssb_freq_start = self.y_start + self.cfg[f'variables.{r}/mod_freq']
            ssb_freq_start_4 = self.frequency_translator(ssb_freq_start)
            
            yinit = f"""
                    move             {ssb_freq_start_4},R6
            """
            self.sequences[r]['program'] += yinit
        
        
    def add_readout(self):
        """
        Instead of using add_gate method, here we directly access the sequencer instruction.
        For more details, please check qtrlb.utils.pulses.
        """
        tof_ns = round(self.cfg.variables['common/tof'] * 1e9)
        length = tof_ns + self.resonator_pulse_length_ns
        
        for tone in self.tones:
            if tone.startswith('Q'):
                readout = f"""
                # -----------Readout-----------
                    wait             {length}
                """
            elif tone.startswith('R'):
                gain = round(self.cfg.variables[f'{tone}/amp'] * 32768)
                readout = f"""
                # -----------Readout-----------
                    set_freq         R6
                    set_awg_gain     {gain},{gain}
                    play             0,0,{tof_ns} 
                    acquire          0,R1,{length - tof_ns}
                """

            self.sequences[tone]['program'] += readout
        
        
    def add_yvalue(self):
        ssb_freq_step_4 = self.frequency_translator(self.y_step)
        for r in self.readout_resonators:  self.sequences[r]['program'] += f"""
                    add              R6,{ssb_freq_step_4},R6    """


    def process_data(self):
        super().process_data(compensate_ED=True)


    def adjust_ED(self, ED: float, save_cfg: bool = True):
        """
        A convenient method for changing electrical delay then redo process, fit and plot_spectrum.
        ED should be in second.
        """
        self.cfg['variables.common/electrical_delay'] = ED
        if save_cfg: 
            self.cfg.save()
            self.cfg.load()
        self.process_data()
        self.plot_spectrum()


class ReadoutAmplitudeScan(ReadoutTemplateScan):
    def __init__(self,
                 cfg: MetaManager, 
                 drive_qubits: str | list[str],
                 readout_resonators: str | list[str],
                 level_start: int,
                 level_stop: int,
                 amp_start: float, 
                 amp_stop: float, 
                 amp_points: int, 
                 pregate: dict[str: list[str]] = None,
                 postgate: dict[str: list[str]] = None,
                 n_seqloops: int = 1000,
                 level_to_fit: int | list[int] = None,
                 fitmodel: Model = None):
        
        super().__init__(cfg=cfg, 
                         drive_qubits=drive_qubits,
                         readout_resonators=readout_resonators,
                         scan_name='ReadoutAmplitude',
                         x_plot_label='Level',
                         x_plot_unit='arb',
                         x_start=level_start,
                         x_stop=level_stop,
                         x_points=level_stop-level_start+1,
                         y_plot_label='Readout Amplitude', 
                         y_plot_unit='arb', 
                         y_start=amp_start, 
                         y_stop=amp_stop, 
                         y_points=amp_points, 
                         pregate=pregate,
                         postgate=postgate,
                         n_seqloops=n_seqloops,
                         level_to_fit=level_to_fit,
                         fitmodel=fitmodel)  
        

    def add_yinit(self):
        super().add_yinit()
        
        for r in self.readout_resonators:
            gain = round(self.y_start * 32768)
            yinit = f"""
                    move             {gain},R6
            """
            self.sequences[r]['program'] += yinit
        
        
    def add_readout(self):
        """
        Instead of using add_gate method, here we directly access the sequencer instruction.
        For more details, please check qtrlb.utils.pulses.
        """
        tof_ns = round(self.cfg.variables['common/tof'] * 1e9)
        length = tof_ns + self.resonator_pulse_length_ns
        
        for tone in self.tones:
            if tone.startswith('Q'):
                readout = f"""
                # -----------Readout-----------
                    wait             {length}
                """
            elif tone.startswith('R'):
                freq = round(self.cfg[f'variables.{tone}/mod_freq'] * 4)
                readout = f"""
                # -----------Readout-----------
                    set_freq         {freq}
                    set_awg_gain     R6,R6
                    play             0,0,{tof_ns} 
                    acquire          0,R1,{length - tof_ns}
                """

            self.sequences[tone]['program'] += readout
        
        
    def add_yvalue(self):
        gain_step = round(self.y_step * 32768)
        for r in self.readout_resonators:  self.sequences[r]['program'] += f"""
                    add              R6,{gain_step},R6    """


class ReadoutLengthAmpScan(ReadoutAmplitudeScan):
    """ Run ReadoutAmplitudeScan with different readout and integration length.
        In principle it should be a 3D scan, but we can only change these integration length \
        in QCoDeS layer, so the easiest way is to make length at the outermost layer. 
        We achieve it by extending 'run' method.
        The plot and possible fit result will be saved in last experiment folder.
        
        Note from Zihao(04/04/2023):
        I do so since I believe the readout length and amplitude have a relatively wide range \
        so that all values within these range can give a reasonable result. 
        Even if noise change the optimal point, it will not be too much.
    """
    def __init__(self,
                 cfg: MetaManager, 
                 drive_qubits: str | list[str],
                 readout_resonators: str | list[str],
                 level_start: int,
                 level_stop: int,
                 amp_start: float, 
                 amp_stop: float, 
                 amp_points: int, 
                 length_start: float = 100 * u.ns,
                 length_stop: float = 5000 * u.ns,
                 length_points: int = 50,
                 pregate: dict[str: list[str]] = None,
                 postgate: dict[str: list[str]] = None,
                 n_seqloops: int = 1000,
                 level_to_fit: int | list[int] = None,
                 fitmodel: Model = None):

        super().__init__(cfg=cfg, 
                         drive_qubits=drive_qubits,
                         readout_resonators=readout_resonators,
                         level_start=level_start,
                         level_stop=level_stop,
                         amp_start=amp_start, 
                         amp_stop=amp_stop, 
                         amp_points=amp_points, 
                         pregate=pregate,
                         postgate=postgate,
                         n_seqloops=n_seqloops,
                         level_to_fit=level_to_fit,
                         fitmodel=fitmodel)

        self.scan_name = 'ReadoutLengthAmp'
        self.length_start = length_start
        self.length_stop = length_stop
        self.length_points = length_points
        self.length_plot_label = 'Readout Length'
        self.length_plot_unit = 'us'
        
        assert 16384 * u.ns > self.length_stop >= self.length_start > 0, \
            'Readout length must be ascending values in (0, 16384) ns.'
        self.length_values = np.linspace(self.length_start, self.length_stop, self.length_points)
        self.length_unit_value = getattr(u, self.length_plot_unit)


    def run(self, 
            experiment_suffix: str = '',
            n_pyloops: int = 1):
        """
        Disassemble the Scan.run() method to save all data into one folder.
        self.data_path will be dynamically changed during loop.
        """
        # Assign attribute as usual
        self.experiment_suffix = experiment_suffix
        self.n_pyloops = n_pyloops
        self.n_reps = self.n_seqloops * self.n_pyloops
        self.attrs = {k: v for k, v in self.__dict__.items() if not k.startswith(('cfg', 'measurement'))}

        # Make the main folder, but not save sequence here since we don't have it yet.
        self.cfg.data.make_exp_dir(experiment_type='_'.join([*self.main_tones_, self.scan_name]),
                                   experiment_suffix=self.experiment_suffix)
        self.main_data_path = self.cfg.data.data_path
        self.datetime_stamp = self.cfg.data.datetime_stamp
        self.cfg.save(yamls_path=self.cfg.data.yamls_path, verbose=False)
        
        # Loop over each length
        for i, length in enumerate(self.length_values):
            # Change length
            self.resonator_pulse_length_ns = round(length * 1e9)
            self.cfg['variables.common/integration_length'] = float(length)

            # Make the sub folder, self.data_path will be updated here.
            self.data_path = os.path.join(self.main_data_path, f'{self.resonator_pulse_length_ns}ns')
            os.makedirs(os.path.join(self.data_path, f'{self.readout_resonators[0]}_IQplots'))
            os.makedirs(os.path.join(self.data_path, 'Jsons'))

            # Run as usual, but using the new self.data_path.
            self.make_sequence() 
            self.save_sequence()
            self.save_sequence(jsons_path=os.path.join(self.data_path, 'Jsons'))
            self.cfg.DAC.implement_parameters(self.tones, self.jsons_path) 
            self.acquire_data()
            self.cfg.data.save_measurement(self.data_path, self.measurement, self.attrs)
            self.process_data()
            self.fit_data()
            self.cfg.data.save_measurement(self.data_path, self.measurement, self.attrs)
            self.plot()
            self.measurements.append(self.measurement)
            
            plt.close('all')
            print(f'RLAS: length_point {i} finish.')
                  
        # Load back the original yaml files.
        # Here you see the power of separating save and set, load and get. :)
        self.cfg.load()
        self.plot_full_result()
        self.n_runs += 1
        
        
    def plot_full_result(self):
        """
        Combine all data with different length and make the 2D plot.
        The figure will be saved in the last experiment folder.
        """
        self.data_all_lengths = {}
        self.figures = {}
        
        for r in self.readout_resonators:
            self.data_all_lengths[r] = []
            
            for measurement in self.measurements:
                self.data_all_lengths[r].append(measurement[r]['to_fit'][0])
                # Index 0 is from process_data in ReadoutTemplateScan.
                
            title = f'{self.datetime_stamp}, Readout Length-Amp Scan, {r}'  
            xlabel = self.y_plot_label + f'[{self.y_plot_unit}]'
            ylabel = self.length_plot_label + f'[{self.length_plot_unit}]'
            
            fig, ax = plt.subplots(1, 1, figsize=(8, 8), dpi=300)
            ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
            
            image = ax.imshow(self.data_all_lengths[r], cmap='RdBu_r', interpolation='none', aspect='auto', 
                              origin='lower', extent=[np.min(self.y_values), 
                                                      np.max(self.y_values), 
                                                      np.min(self.length_values) / self.length_unit_value, 
                                                      np.max(self.length_values) / self.length_unit_value])
            fig.colorbar(image, ax=ax, label='Fidelity', location='top')
            fig.savefig(os.path.join(self.main_data_path, f'{r}_full_result.png'))
            self.figures[r] = fig

        
class DRAGWeightScan(Scan2D):
    def __init__(self, 
                 cfg: MetaManager, 
                 drive_qubits: str | list[str], 
                 readout_resonators: str | list[str], 
                 weight_start: float, 
                 weight_stop: float, 
                 weight_points: int, 
                 subspace: str | list[str] = None, 
                 main_tones: str | list[str] = None, 
                 pregate: dict[str: list[str]] = None, 
                 postgate: dict[str: list[str]] = None, 
                 n_seqloops: int = 1000, 
                 level_to_fit: int | list[int] = None, 
                 fitmodel: Model = QuadModel):
        super().__init__(cfg=cfg, 
                         drive_qubits=drive_qubits, 
                         readout_resonators=readout_resonators, 
                         scan_name='DRAGWeight', 
                         x_plot_label='Pulse Order', 
                         x_plot_unit='arb', 
                         x_start=0, 
                         x_stop=1, 
                         x_points=2, 
                         y_plot_label='DRAG Weight', 
                         y_plot_unit='arb', 
                         y_start=weight_start, 
                         y_stop=weight_stop, 
                         y_points=weight_points, 
                         subspace=subspace, 
                         main_tones=main_tones, 
                         pregate=pregate, 
                         postgate=postgate, 
                         n_seqloops=n_seqloops, 
                         level_to_fit=level_to_fit, 
                         fitmodel=fitmodel)
        
        for tone in self.main_tones:
            assert -1 <= self.cfg[f'variables.{tone}/amp_180'] * self.y_start < 1, 'Start value exceed range.'
            assert -1 <= self.cfg[f'variables.{tone}/amp_180'] * self.y_stop < 1, 'Stop value exceed range.'


    def add_yinit(self):
        """
        Here R6 is the gain on the DRAG path, which is the gain on main path times DRAG_weight.
        R12 is half of R6 for the half PI pulse.
        R11 is just the gain on main path, and R13 is half of R11, they won't change during program. 
        It's because set_awg_gain only take '#,#' or 'R#,R#' format, not '#,R#'.
        """
        super().add_yinit()

        for tone in self.main_tones:
            # A value between [0, 1)
            gain_raw = self.cfg[f'variables.{tone}/amp_180']

            # Values in 32768 format.
            gain = self.gain_translator(gain_raw)
            gain_half = self.gain_translator(gain_raw / 2)

            gain_drag_start = self.gain_translator(gain_raw * self.y_start)
            gain_drag_half_start = self.gain_translator(gain_raw * self.y_start / 2)

            yinit = f"""
                    move             {gain_drag_start},R6
                    move             {gain},R11
                    move             {gain_drag_half_start},R12
                    move             {gain_half},R13
            """
            self.sequences[tone]['program'] += yinit


    def add_main(self):
        """
        Here I use jge and jlt instruction to realize the conditional instructions.
        For more information, please refer to:
        https://qblox-qblox-instruments.readthedocs-hosted.com/en/master/documentation/sequencer.html
        """

        for tone in self.main_tones:
            ssb_freq = self.cfg[f'variables.{tone}/mod_freq'] + self.cfg[f'variables.{tone}/pulse_detuning']
            ssb_freq_4 = round(ssb_freq * 4)

            main = f"""
                    jge              R3,2,@XpiYhalf
                    jlt              R3,2,@YpiXhalf

        XpiYhalf:
                    set_freq         {ssb_freq_4}
                    set_awg_gain     R11,R6
                    play             0,1,{self.qubit_pulse_length_ns} 

                    set_ph_delta     {round(750e6)}
                    set_awg_gain     R13,R12
                    play             0,1,{self.qubit_pulse_length_ns}
                    set_ph_delta     {round(250e6)}

                    jmp              @end_main

        YpiXhalf:
                    set_ph_delta     {round(750e6)}
                    set_awg_gain     R13,R12
                    play             0,1,{self.qubit_pulse_length_ns}
                    set_ph_delta     {round(250e6)}

                    set_freq         {ssb_freq_4}
                    set_awg_gain     R11,R6
                    play             0,1,{self.qubit_pulse_length_ns} 

                    jmp              @end_main

        end_main:
            """
            self.sequences[tone]['program'] += main

        for tone in self.rest_tones:
            main = f"""
                    wait             {self.qubit_pulse_length_ns * 2}
            """
            self.sequences[tone]['program'] += main


    def add_yvalue(self):
        for tone in self.main_tones:
            gain_raw = self.cfg[f'variables.{tone}/amp_180']
            
            gain_drag_step = self.gain_translator(gain_raw * self.y_step)
            gain_drag_step_half = self.gain_translator(gain_raw * self.y_step / 2)

            self.sequences[tone]['program'] += f"""
                    add              R6,{gain_drag_step},R6
                    add              R12,{gain_drag_step_half},R12
            """


    def process_data(self):
        """
        Here we do a further step of processing data by not make 'to_fit' as (n_levels, y_points, x_points), \
        but (n_levels, y_points) where we take difference between the two x_points.
        """
        super().process_data()
        for r, data_dict in self.measurement.items(): 
            data_dict['to_fit_raw'] = data_dict['to_fit']
            data_dict['to_fit'] = (data_dict['to_fit_raw'][..., 0] - data_dict['to_fit_raw'][..., 1]) ** 2


    def fit_data(self):
        super().fit_data(x=self.y_values)  # We're actually using 1D fitting Scan.fit_data().


    def plot_main(self, text_loc: str = 'lower right'):
        """
        Plot the difference between two x_points for each DRAG weight.
        Code is similar to Scan.plot_main()
        """
        self.figures = {}
        
        for i, r in enumerate(self.readout_resonators):
            level_index = self.level_to_fit[i] - self.cfg[f'variables.{r}/lowest_readout_levels']      
            title = f'{self.datetime_stamp}, {self.scan_name}, {r}'
            xlabel = self.y_plot_label + f'[{self.y_plot_unit}]'
            if self.classification_enable:
                ylabel = fr'Difference of $P_{{\left|{self.level_to_fit[i]}\right\rangle}}$'
            else:
                ylabel = 'Difference of I-Q Coordinate (Rotated) [a.u.]'
            
            fig, ax = plt.subplots(1, 1, dpi=150)
            ax.plot(self.y_values / self.y_unit_value, self.measurement[r]['to_fit'][level_index], 'k.')
            ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
            
            if self.fit_result[r] is not None: 
                # Raise resolution of fit result for smooth plot.
                x = np.linspace(self.y_start, self.y_stop, self.y_points * 3)  
                y = self.fit_result[r].eval(x=x)
                ax.plot(x / self.y_unit_value, y, 'm-')
                
                # AnchoredText stolen from Ray's code.
                fit_text = '\n'.join([f'{v.name} = {v.value:0.5g}' for v in self.fit_result[r].params.values()])
                anchored_text = AnchoredText(fit_text, loc=text_loc, prop={'color':'m'})
                ax.add_artist(anchored_text)

            fig.savefig(os.path.join(self.data_path, f'{r}.png'))
            self.figures[r] = fig