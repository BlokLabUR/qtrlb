import os
from qtrlb.config.config import Config
from qtrlb.config.variable_manager import VariableManager
from qblox_instruments import Cluster


class DACManager(Config):
    """ This is a thin wrapper over the Config class to help with DAC management.
        The load() method will be called once in its __init__.
    
        Attributes:
            yamls_path: An absolute path of the directory containing all yamls with a template folder.
            varman: A VariableManager.
            test_mode: When true, you can run the whole program without a real instrument.
    """
    def __init__(self, 
                 yamls_path: str, 
                 varman: VariableManager = None,
                 test_mode: bool = False):
        super().__init__(yamls_path=yamls_path, 
                         suffix='DAC',
                         varman=varman)
        
        # Connect to instrument. Hardcode name and IP address to accelerate load()
        Cluster.close_all()
        self.test_mode = test_mode

        if self.test_mode:
            dummy_cfg = {2:'Cluster QCM-RF', 4:'Cluster QCM-RF', 6:'Cluster QCM-RF', 8:'Cluster QRM-RF'}
            self.qblox = Cluster(name='cluster', dummy_cfg=dummy_cfg)
        else:
            self.qblox = Cluster('cluster', '192.168.0.2') 

        self.qblox.reset()
        
        self.load()
    
    
    def load(self):
        """
        Run the parent load and add necessary new item in config_dict.
        """
        super().load()
        
        modules_list = [key for key in self.keys() if key.startswith('Module')]
        self.set('modules', modules_list, which='dict')  # Keep the new key start with lowercase!
        
        # These dictionary contain pointer to real object in instrument driver.
        self.module = {}
        self.sequencer = {}
        for tone in self.varman['tones']:
            qudit = self.tone_to_qudit(tone)
            self.module[qudit] = getattr(self.qblox, 'module{}'.format(self.varman[f'{qudit}/module']))
            self.sequencer[tone] = getattr(self.module[qudit], 'sequencer{}'.format(self.varman[f'{tone}/sequencer']))
             
        
    def implement_parameters(self, tones: list, jsons_path: str):
        """
        Implement the setting/parameters onto Qblox.
        This function should be called after we know which specific tones will be used.
        The tones should be list of string like: ['Q3/01', 'Q3/12', 'Q3/23', 'Q4/01', 'Q4/12', 'R3', 'R4'].
        
        Right now it's just a temporary way to null the mixer.
        We need to manually get each parameter and save it to DAC.yaml file.
        In future we should have automatic mixer nulling from Spectral Analyzer feedback. --Zihao(04/12/2023)
        """
        self.qblox.reset()
        self.disconnect_existed_map()
        
        for tone in tones:
            tone_ = tone.replace('/', '_')
            qudit = tone.split('/')[0]
            module_idx = self.varman[f'{qudit}/module']  # Just an interger. It's for convenience.
            sequencer_idx = self.varman[f'{tone}/sequencer']

            # Implement common parameters.
            for attribute in self[f'Module{module_idx}'].keys():
                if attribute.startswith(('out', 'in', 'scope')):
                    getattr(self.module[qudit], attribute)(self[f'Module{module_idx}/{attribute}'])

            # Implement QCM-RF specific parameters.
            if qudit.startswith('Q'):
                out = self.varman[f'{qudit}/out']
                getattr(self.module[qudit], f'out{out}_lo_freq')(self.varman[f'{qudit}/qubit_LO']) 

                self.sequencer[tone].sync_en(True)
                self.sequencer[tone].mod_en_awg(True)

                getattr(self.sequencer[tone], f'channel_map_path0_out{out*2}_en')(True)
                getattr(self.sequencer[tone], f'channel_map_path1_out{out*2+1}_en')(True)
                
            # Implement QRM-RF specific parameters.
            elif qudit.startswith('R'):
                self.module[qudit].out0_in0_lo_freq(self.varman[f'{qudit}/resonator_LO'])        
                self.module[qudit].scope_acq_sequencer_select(self.varman[f'{qudit}/sequencer'])  
                # Last sequencer to triger acquire.
                self.sequencer[tone].sync_en(True)
                self.sequencer[tone].mod_en_awg(True)
                self.sequencer[tone].demod_en_acq(True)
                self.sequencer[tone].integration_length_acq(round(self.varman['common/integration_length'] * 1e9))
                self.sequencer[tone].nco_prop_delay_comp_en(self.varman['common/nco_delay_comp'])
                self.sequencer[tone].channel_map_path0_out0_en(True)
                self.sequencer[tone].channel_map_path1_out1_en(True)
                  

            self.sequencer[tone].mixer_corr_gain_ratio(
                self[f'Module{module_idx}/Sequencer{sequencer_idx}/mixer_corr_gain_ratio']
                )           
            self.sequencer[tone].mixer_corr_phase_offset_degree(
                self[f'Module{module_idx}/Sequencer{sequencer_idx}/mixer_corr_phase_offset_degree']
                )
            
            file_path = os.path.join(jsons_path, f'{tone_}_sequence.json')
            self.sequencer[tone].sequence(file_path)
    
    
    def disconnect_existed_map(self):
        """
        Disconnect all existed maps between two output paths of each output port 
        and two output paths of each sequencer.
        """
        for m in self['modules']:
            this_module = getattr(self.qblox, f'{m}'.lower())
            
            # Steal code from Qblox official tutoirals.
            if self[f'{m}/type'] == 'QCM-RF':
                for sequencer in this_module.sequencers:
                    for out in range(0, 4):
                        if hasattr(sequencer, "channel_map_path{}_out{}_en".format(out%2, out)):
                            sequencer.set("channel_map_path{}_out{}_en".format(out%2, out), False)
                    
            elif self[f'{m}/type'] == 'QRM-RF':
                for sequencer in this_module.sequencers:
                    for out in range(0, 2):
                        if hasattr(sequencer, "channel_map_path{}_out{}_en".format(out%2, out)):
                            sequencer.set("channel_map_path{}_out{}_en".format(out%2, out), False)
                
            else:
                raise ValueError(f'The type of {m} is invalid.')


    def start_sequencer(self, tones: list, measurement: dict, keep_raw: bool = False):
        """
        Ask the instrument to start sequencer.
        Then store the Heterodyned result into measurement.
        
        Reference about data structure:
        https://qblox-qblox-instruments.readthedocs-hosted.com/en/master/api_reference/cluster.html#qblox_instruments.native.Cluster.get_acquisitions
        
        Note from Zihao(02/15/2023):
        We need to call delete_scope_acquisition everytime.
        Otherwise the binned result will accumulate and be averaged automatically for next repetition.
        Within one repetition (one round), we can only keep raw data from last bin (last acquire instruction).
        Which means for Scan, only the raw trace belong to last point in x_points will be stored.
        So it's barely useful, but I still leave the interface here.
        """
        # Arm sequencer first. It's necessary. Only armed sequencer will be started next.
        for tone in tones:
            self.sequencer[tone].arm_sequencer()
            
        # Really start sequencer.
        self.qblox.start_sequencer()  

        for r in tones:
            # Only loop over resonator.
            if not r.startswith('R'): continue

            timeout = self['Module{}/acquisition_timeout'.format(self.varman[f'{r}/module'])]
            seq_idx = self.varman[f'{r}/sequencer']
           
            # Wait the timeout in minutes and ask whether the acquisition finish on that sequencer. Raise error if not.
            self.module[r].get_acquisition_state(seq_idx, timeout)  

            # Store the raw (scope) data from buffer of FPGA to RAM of instrument.
            if keep_raw: 
                self.module[r].store_scope_acquisition(seq_idx, 'readout')
                self.module[r].store_scope_acquisition(seq_idx, 'heralding')
            
            # Retrive the heterodyned result (binned data) back to python in Host PC.
            data = self.module[r].get_acquisitions(seq_idx)
            
            # Clear the memory of instrument. 
            # It's necessary otherwise the acquisition result will accumulate and be averaged.
            self.module[r].delete_acquisition_data(seq_idx, 'readout')
            self.module[r].delete_acquisition_data(seq_idx, 'heralding')
            
            # Append list of each repetition into measurement dictionary.
            measurement[r]['Heterodyned_readout'][0].append(data['readout']['acquisition']['bins']['integration']['path0']) 
            measurement[r]['Heterodyned_readout'][1].append(data['readout']['acquisition']['bins']['integration']['path1'])
            measurement[r]['Heterodyned_heralding'][0].append(data['heralding']['acquisition']['bins']['integration']['path0']) 
            measurement[r]['Heterodyned_heralding'][1].append(data['heralding']['acquisition']['bins']['integration']['path1'])

            if keep_raw:
                measurement[r]['raw_readout'][0].append(data['readout']['acquisition']['scope']['path0']['data']) 
                measurement[r]['raw_readout'][1].append(data['readout']['acquisition']['scope']['path1']['data'])
                measurement[r]['raw_heralding'][0].append(data['heralding']['acquisition']['scope']['path0']['data']) 
                measurement[r]['raw_heralding'][1].append(data['heralding']['acquisition']['scope']['path1']['data'])


    @staticmethod
    def tone_to_qudit(tone: str | list) -> str | list:
        """
        Translate the tone to qudit.
        Accept a string or a list of string.
        It's because qudit is mapping to module and tone is mapping to sequencer.

        Example:
        tone_to_qudit('Q2') -> 'Q2'
        tone_to_qudit('R2') -> 'R2'
        tone_to_qudit('Q2/12') -> 'Q2'
        tone_to_qudit(['Q2/01', 'Q2/12', 'R2']) -> ['Q2', 'R2']
        tone_to_qudit([['Q2/01', 'Q3/12', 'R2'],['Q2/01', 'Q2/12', 'R2']]) -> [['Q2', 'Q3', 'R2'], ['Q2', 'R2']]
        """
        if isinstance(tone, str):
            assert tone.startswith(('Q', 'R')), f'DAC: Cannot translate {tone} to qudit.'
            try:
                qudit, _ = tone.split('/')
                return qudit
            except ValueError:
                return tone
            
        elif isinstance(tone, list):
            qudit = []
            for t in tone:
                q = DACManager.tone_to_qudit(t)
                if q not in qudit: qudit.append(q)
            return qudit

        else:
            raise TypeError(f'DAC: Cannot translate the {tone}. Please check it type.')

