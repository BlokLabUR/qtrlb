import os
import json
import numpy as np
import pandas as pd
from qtrlb.utils.waveforms import get_waveform
from qtrlb.utils.pulses import pulse_interpreter



class Scan:
    """ Base class for all parameter-sweep experiment.
        The framework of how experiment flow will be constructed here.
        It should be used as parent class of specific scan rather than being instantiated directly.
        
        Attributes:
            cfg: A MetaManager.
            drive_qubits: 'Q2', or ['Q3', 'Q4'].
            readout_resonators: 'R3' or ['R1', 'R5'].
            x_name: 'drive_amplitude', 't1', 'ramsey'.
            x_start: 0 or [0.5, 1.5], length should be same as drive_qubits.
            x_stop: 10 or [10.5, 11.5], length should be same as drive_qubits.
            x_points: number of points on x_axis. Start and stop points will be both included.
            subspace: '12' or ['01', '01'], length should be same as drive_qubits.
            prepulse: {'Q0': ['X180_01'], 'Q1': ['X90_12', 'Y90_12']}
            postpulse: Same requirement as prepulse.
    """
    def __init__(self, 
                 cfg, 
                 drive_qubits: str | list,
                 readout_resonators: str | list,
                 x_name: str,
                 # x_label: str, 
                 # x_unit: str, 
                 x_start: float | list, 
                 x_stop: float | list, 
                 x_points: int, 
                 subspace: list = None,
                 prepulse: dict = None,
                 postpulse: dict = None,
                 fitmodel = None):
        self.cfg = cfg
        self.drive_qubits = self.make_it_list(drive_qubits)
        self.readout_resonators = self.make_it_list(readout_resonators)
        self.x_name = x_name
        # self.x_label = x_label
        # self.x_unit = x_unit
        self.x_start = self.make_it_list(x_start)
        self.x_stop = self.make_it_list(x_stop)
        self.x_points = x_points
        self.subspace = subspace if subspace is not None else ['01']*len(drive_qubits)
        self.prepulse = prepulse if prepulse is not None else {}
        self.postpulse = postpulse if postpulse is not None else {}
        self.fitmodel = fitmodel
        
        self.n_runs = 0
        self.measurements = []
        self.qudits = self.drive_qubits + self.readout_resonators
        self.classification_enable = self.cfg.variables['common/classification']
        self.heralding_enable = self.cfg.variables['common/heralding']
        
        self.check_attribute()
        self.x_values = np.linspace(self.x_start, self.x_stop, self.x_points).transpose().tolist()
        self.x_step = [(stop-start)/(self.x_points-1) for start, stop in zip(self.x_start, self.x_stop)]
        
        self.generate_pulse_dataframe()
        self.make_sequence() 
        jsons_path = self.save_sequence()
        self.cfg.DAC.implement_parameters(qubits=self.drive_qubits, 
                                          resonators=self.readout_resonators,
                                          subspace=self.subspace,
                                          jsons_path=jsons_path)
        # Configure the Qblox to desired parameters then upload json files.
        # We call implement_parameters methods here instead of during init/load of DACManager,
        # because we want those modules/sequencers not being used to keep their default status.
        
        
    def run(self, 
            experiment_suffix: str = '',
            n_reps: int  = 1000):
        self.experiment_suffix = experiment_suffix
        self.n_reps = n_reps
        
        self.make_exp_dir()
        self.acquire_data()  # This is really run the thing and return to the IQ data in self.measurement.
        self.cfg.data.save_measurement(data_path=self.data_path,
                                       measurement=self.measurement)
        
        self.process_data()  # Need the ProcessManager, hardcode three common situation.
        self.plot()
        self.n_runs += 1
        self.measurements.append(self.measurement)
        
        
    def check_attribute(self):
        """
        Check the qubits/resonators are always string with 'Q' or 'R'.
        Warn user if any drive_qubits are not being readout without raising error.
        Make sure each qubit has a scan_start, scan_stop, subspace.
        Make sure the prepulse/postpulse is indeed in form of dictionary.
        """
        for qudit in self.qudits:
            assert isinstance(qudit, str), f'The type of {qudit} is not a string!'
            assert qudit.startswith('Q') or qudit.startswith('R'), f'The value of {qudit} is invalid.'
            
        for qubit in self.drive_qubits:
            if f'R{qubit[1:]}' not in self.readout_resonators: print(f'Scan: The {qubit} will not be readout!')
        
        assert len(self.x_start) == len(self.drive_qubits), 'Please specify scan_start for each qubit.'
        assert len(self.x_stop) == len(self.drive_qubits), 'Please specify scan_stop for each qubit.'
        assert len(self.subspace) == len(self.drive_qubits), 'Please specify subspace for each qubit.'
        assert isinstance(self.prepulse, dict), 'Prepulse must be dictionary like {"Q0":[pulse1, pulse2,...]}'
        assert isinstance(self.postpulse, dict), 'Postpulse must to be dictionary like {"Q0":[pulse1, pulse2,...]}'


    def generate_pulse_dataframe(self):
        """
        Generate the Pandas DataFrame of prepulse, postpulse, readout, with padded 'I'.
        Both subspace and input prepulse will be included into prepulse.
        All qubits and resonators will become the (row) index of dataframe.
        An additional interger attribute 'length' in [ns] will be associated with each column.
        
        Example of full prepulse DataFrame:
           subspace_0 subspace_1 prepulse_0 prepulse_1
        Q3    X180_01          I     Y90_01          I
        Q4    X180_01    X180_12     Y90_01     Z90_12
        R3          I          I          I          I
        R4          I          I          I          I
        """
        # Generate subspace pulse and readout dict
        self.subspace_pulse = {}
        for q, ss in zip(self.drive_qubits, self.subspace):
            self.subspace_pulse[q] = [f'X180_{l}{l+1}' for l in range(int(ss[0]))]

        self.readout_pulse = {r: ['RO'] for r in self.readout_resonators}
        
        self.prepulse_df = self.dict_to_DataFrame(self.prepulse, 'prepulse', self.qudits)
        self.subspace_df = self.dict_to_DataFrame(self.subspace_pulse, 'subspace', self.qudits)
        self.postpulse_df = self.dict_to_DataFrame(self.postpulse, 'postpulse', self.qudits)
        self.readout_df = self.dict_to_DataFrame(self.readout_pulse, 'readout', self.qudits)         
        self.full_prepulse_df = pd.concat([self.subspace_df, self.prepulse_df], axis=1)
            
        
        readout_length_ns = round(self.cfg.variables['common/resonator_pulse_length'] * 1e9)
        tof_ns = round(self.cfg.variables['common/tof'] * 1e9)    
        drive_length_ns = round(self.cfg.variables['common/qubit_pulse_length'] * 1e9)
        
        # Assign the length attribute to each column.
        for col_name, column in self.full_prepulse_df.items(): column.length = drive_length_ns
        for col_name, column in self.postpulse_df.items(): column.length = drive_length_ns
        for col_name, column in self.readout_df.items(): column.length = tof_ns + readout_length_ns
        # I agree it's not very general here, since we assume everything in prepulse/postpulse is qubit gate.
        # Thus we pad the dataframe with Indentity but all with qubit's gate time.
        # It's will break the sync between sequencers when we have any pulse that is not exactly that time.
        # I believe we can deal with special pulse when we really meet them.
        # For example, such experiment should be a child class with redefined add_prepulse, or add_pulse to whole sequence.
        # Right now I just want to make things work first, then make them better. --Zihao(02/06/2023)


    ##################################################
    def make_sequence(self):
        """
        Generate the self.sequences, which is a dictionary including all sequence dictionaries
        we will dump to json file.
        
        Example:
        self.sequences = {'Q3': Q3_sequence_dict,
                          'Q4': Q4_sequence_dict,
                          'R3': R3_sequence_dict,
                          'R4': R4_sequence_dict}
        
        Each sequence dictionary should looks like:
        sequence_dict = {'waveforms': waveforms,
                         'weights': weights,
                         'acquisitions': acquisitions,
                         'program': seq_prog}
        
        Please check the link below for detail:
        https://qblox-qblox-instruments.readthedocs-hosted.com/en/master/tutorials/basic_sequencing.html
        """
        self.sequences = {qudit:{} for qudit in self.qudits}        
        
        self.set_waveforms_acquisitions()
        
        self.init_program()
        self.add_initparameter()
        self.add_mainloop()
        self.add_relaxation()
        if self.heralding_enable: self.add_heralding()
        self.add_prepulse()
        self.add_mainpulse()
        self.add_postpulse()
        self.add_readout()
        self.add_stop()
    
        
    def set_waveforms_acquisitions(self):
        """
        Generate waveforms, weights, acquisitions items in self.sequences[qudit].
        
        We skip weights acquisition, and I believe even if we need it,
        we can do it in post process. --Zihao(01/31/2023)
        Please check the link below for detail:
        https://qblox-qblox-instruments.readthedocs-hosted.com/en/master/tutorials/binned_acquisition.html
        """
        for qudit in self.qudits:
            pulse_type = 'qubit' if qudit.startswith('Q') else 'resonator'
            
            waveform = get_waveform(round(self.cfg.variables[f'common/{pulse_type}_pulse_length'] * 1e9), 
                                    self.cfg.variables[f'{qudit}/pulse_shape'])
            
            waveforms = {qudit: {'data': waveform, 'index': 0}}
            
            acquisitions = {'readout':   {'num_bins': self.x_points, 'index': 0},
                            'heralding': {'num_bins': self.x_points, 'index': 1}}
            
            self.sequences[qudit]['waveforms'] = waveforms
            self.sequences[qudit]['weights'] = {}
            self.sequences[qudit]['acquisitions'] = acquisitions           

        
    def init_program(self):
        for qudit in self.qudits:
            program = """
        # R0 is the value of main parameter of 1D Scan, if needed.
        # R1 is the count of repetition for algorithm or x_points for parameter sweep.
        # R2 is the relaxation time in microseconds.
        # Other register for backup.
        
                    wait_sync        8
                    move             0,R0
                    move             0,R1
                    move             0,R2
                    move             0,R3
                    move             0,R4
                    move             0,R5
        """
            self.sequences[qudit]['program'] = program
        
        
    def add_initparameter(self):
        """
        Suppose to be called by child class.
        """
        print('Scan: The base experiment class has been called. No initial parameter will be set.')
        
        
    def add_mainloop(self):
        for qudit in self.qudits:
            loop = """        
        main_loop:  wait_sync        8                               # Sync at beginning of the loop.
                    reset_ph                                         # Reset phase to eliminate effect of previous VZ gate.
                    set_mrk          15                              # Enable all markers (binary 1111) for switching on output.
                    upd_param        8                               # Update parameters and wait 8ns.
        """
            self.sequences[qudit]['program'] += loop
        
        
    def add_relaxation(self):
        relaxation_time_s = self.cfg.variables['common/relaxation_time']
        relaxation_time_us = int( np.ceil(relaxation_time_s*1e6) )
        relaxation = f"""
                #-----------Relaxation-----------
                    move             {relaxation_time_us},R2
        relx_loop:  wait             1000
                    loop             R2,@relx_loop
        """
        for qudit in self.qudits: self.sequences[qudit]['program'] += relaxation
        
        
    def add_heralding(self, acq_index: int = 1):
        heralding_delay = self.cfg.variables['common/heralding_delay']
        self.add_readout(acq_index=acq_index)
        self.add_wait(time=heralding_delay)
        

    def add_prepulse(self):
        for col_name, column in self.full_prepulse_df.items():
            for qudit in self.qudits:
                prepulse = """
                #-----------Prepulse-----------
                """
                init_pulse_str = column[qudit]
                prepulse += pulse_interpreter(cfg = self.cfg, 
                                              qudit = qudit, 
                                              pulse_string = init_pulse_str, 
                                              length = column.length)
                self.sequences[qudit]['program'] += prepulse
        

    def add_mainpulse(self):        
        """
        Suppose to be called by child class.
        """
        print('Scan: The base experiment class has been called. No main pulse will be added.')

        
    def add_postpulse(self):
        for col_name, column in self.postpulse_df.items():
            for qudit in self.qudits:
                postpulse = """
                #-----------Postpulse-----------
                """
                init_pulse_str = column[qudit]
                postpulse += pulse_interpreter(cfg = self.cfg, 
                                               qudit = qudit, 
                                               pulse_string = init_pulse_str, 
                                               length = column.length)
                self.sequences[qudit]['program'] += postpulse
    
    
    def add_readout(self, acq_index: int = 0):
        for col_name, column in self.readout_df.items():
            for qudit in self.qudits:
                readout = """
                #-----------Readout-----------
                """
                init_pulse_str = column[qudit]
                readout += pulse_interpreter(cfg = self.cfg, 
                                             qudit = qudit, 
                                             pulse_string = init_pulse_str, 
                                             length = column.length,
                                             acq_index = acq_index)
                self.sequences[qudit]['program'] += readout
            

    def add_stop(self):
        stop = f"""
                #-----------Stop-----------
                    add              R1,1,R1
                    set_mrk          0                               # Disable all markers (binary 0000) for switching off output.
                    upd_param        8                               # Update parameters and wait 4ns.
                    jlt              R1,{self.x_points},@main_loop
                    
                    stop             
        """
        for qudit in self.qudits: self.sequences[qudit]['program'] += stop


    def add_wait(self, time: float):
        """
        The time parameter should be in unit of [sec]
        """
        time_ns = round(time * 1e9)
        assert time_ns < 65535 & time_ns >= 4, 'The wait time can only be in [4,65535).'
        wait = f"""
        #-----------Wait-----------
                    wait             {time_ns}                               
        """
        for qudit in self.qudits: self.sequences[qudit]['program'] += wait
    
            
    def add_pulse(self, pulse: dict, lengths: list, name: str = 'pulse'):
        lengths = self.make_it_list(lengths)
        pulse_df = self.dict_to_DataFrame(pulse, name, self.qudits)
        assert len(lengths) == pulse_df.shape[1], 'You need to specify length [ns] for each column!'
        
        for col_name, column in pulse_df.items():
            name, index = col_name.split('_')
            column.length = lengths[index]
            for qudit in self.qudits:
                pulse_prog = f"""
                #-----------{name}-----------
                """
                init_pulse_str = column[qudit]
                pulse_prog += pulse_interpreter(cfg = self.cfg, 
                                                qudit = qudit, 
                                                pulse_string = init_pulse_str, 
                                                length = column.length)
                self.sequences[qudit]['program'] += pulse_prog
        
        
    ##################################################    
    def save_sequence(self, jsons_path: str = None):
        """
        Create json file of sequence for each sequencer/qudit and save it.
        Allow user to pass a path of directory to save jsons at another place.
        """
        if jsons_path is None:
            jsons_path = os.path.join(self.cfg.working_dir, 'Jsons') 

        for qudit, sequence_dict in self.sequences.items():
            file_path = os.path.join(jsons_path, f'{qudit}_sequence.json')
            with open(file_path, 'w', encoding='utf-8') as file:
                json.dump(sequence_dict, file, indent=4)
                
        return jsons_path
            
    
    def make_exp_dir(self):
        """
        Create an experiment directory under cfg.data.base_directory.
        Then save a copy of jsons and yamls to experiment directory.
        
        Note from Zihao(02/14/2023)
        If sequence program has problem, then __init__() will raise Error, rather than run().
        In that case we won't create the junk experiment folder.
        If problem happen after we start_sequencer, then it worth to create the experiment folder.
        Because we might already get some data and want to save it.
        """
        self.data_path = self.cfg.data.make_exp_dir(experiment_type=self.x_name,
                                                    experiment_suffix=self.experiment_suffix)
        
        self.cfg.save(yamls_path=os.path.join(self.data_path, 'Yamls'))
        self.save_sequence(jsons_path=os.path.join(self.data_path, 'Jsons'))
    
    
    def acquire_data(self):
        """
        Create measurement dictionary, then start sequencer and save data into this dictionary.
        """
        self.measurement = {r: {'raw_readout': [[],[]],  # First element for I, second element for Q.
                                'raw_heralding': [[],[]],
                                'Heterodyned_readout': [[],[]],
                                'Heterodyned_heralding':[[],[]]
                                } for r in self.readout_resonators}
        
        for i in range(self.n_reps):
            self.cfg.DAC.start_sequencer(qubits=self.drive_qubits,
                                         resonators=self.readout_resonators,
                                         measurement=self.measurement,
                                         heralding_enable=self.heralding_enable)
            
            
    def process_data(self):
        """

        """
        # TODO: Determine what process we need to implement for three common situation.
        # Or may be let ProcessManager to determine it.
        if self.heralding_enable:
            self.process_list = []
        elif self.classification_enable:
            self.process_list = []
        else:
            self.process_list = []


    def plot(self):
        pass

        
    @staticmethod
    def make_it_list(thing):
        """
        A crucial, life-saving function.
        """
        if isinstance(thing, list):
            return thing
        elif thing == None:
            return []
        else:
            return [thing]


    @staticmethod
    def dict_to_DataFrame(dic: dict, name: str, rows: list, padding: object = 'I'):
        """
        Turn a dictionary into a Pandas DataFrame with padding.
        Each key in dic or element in rows will become index (row) of the DataFrame.
        Each column will be renamed as 'name_0', 'name_1'.
        
        Example:
            dict: {'Q3':['X180_01', 'X180_12'], 'Q4':['Y90_01']}
            name: 'prepulse'
            rows: ['Q3', 'Q4', 'R3', 'R4']
        """
        for row in rows:
            if row not in dic: dic[row] = []
            
        dataframe = pd.DataFrame.from_dict(dic, orient='index')
        dataframe = dataframe.rename(columns={i:f'{name}_{i}' for i in range(dataframe.shape[1])})
        dataframe = dataframe.fillna(padding)        
        return dataframe









































