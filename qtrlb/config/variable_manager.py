import qtrlb.utils.units as u
from qtrlb.config.config import Config


class VariableManager(Config):
    """ This is a thin wrapper over the Config class to help with variables management.
        The load() method will be called once in its __init__.
    
        Attributes:
            yamls_path: An absolute path of the directory containing all yamls with a template folder.
            variable_suffix: 'EJEC' or 'ALGO'. A underscroll will be added in this layer.
    """
    def __init__(self, 
                 yamls_path: str, 
                 variable_suffix: str = ''):
        super().__init__(yamls_path=yamls_path, 
                         suffix='variables',
                         variable_suffix='_'+variable_suffix)
        self.load()
        
    
    def load(self):
        """
        Run the parent load, then generate a seires of items in config_dict
        including tones, mod_freq for NCO, anharmonicity, n_readout_levels.

        Note from Zihao(11/01/2023):
        In this version I choose never to check check LO and sequencer confliction.
        It's because it didn't happen to me at all and in new structure there isn't too much to check.
        """
        super().load()
        self.set_parameters()
   
    
    def set_parameters(self):
        """
        Set mod_freq for NCO, anharmonicity, n_readout_levels, etc, into config_dict.
        Most importantly, it will generate the key 'tones' whose value looks like: 
        ['Q0/01', 'Q0/12', 'Q1/01', 'Q1/ACStark', 'R0', 'R1a', 'R1b'].
        mod, out, seq are strings of integer represent the index of module, output port, sequencer.
        """
        tones_list = []

        for qudit in self.keys():
            if qudit.startswith('Q'):
                for subtone, tone_dict in self[f'{qudit}'].items():
                    # Add tone
                    tone = f'{qudit}/{subtone}'
                    tones_list.append(tone)

                    # Set mod, out, seq for convenience.
                    mod, out, seq = tone_dict['sequencer'].split('/')
                    self.set(f'{tone}/mod', int(mod), which='dict')
                    self.set(f'{tone}/out', int(out), which='dict')
                    self.set(f'{tone}/seq', int(seq), which='dict')

                    # Check the DRAG_weight is in range.
                    assert -1 <= tone_dict['amp_180'] * tone_dict['DRAG_weight'] < 1, \
                        f'Varman: DRAG weight of {qudit}/{tone} is out of range.'
                    
                    # Set NCO frequency for each tone.
                    mod_freq = self[f'{tone}/freq'] - self[f'lo_freq/M{mod}O{out}']
                    assert -500*u.MHz < mod_freq < 500*u.MHz, f'Varman: mod_freq of {tone} is out of range.'
                    self.set(f'{tone}/mod_freq', mod_freq, which='dict')
                    
                    # Set anharmonicity for each subspace.
                    # It can solve subspace like '910' and compare '02' with '01'.
                    if (not subtone.isdecimal()) or subtone == '01': continue
                    level_high = int( subtone[ int(len(subtone)/2) : ] )
                    last_tone = f'{qudit}/{level_high - 2}{level_high - 1}'
                    anharmonicity = self[f'{tone}/freq'] - self[f'{last_tone}/freq']
                    self.set(f'{tone}/anharmonicity', anharmonicity, which='dict')

            elif qudit.startswith('R'):
                tone = qudit
                tones_list.append(tone)

                # Set mod, out, seq for convenience.
                mod, out, seq = self[f'{tone}/sequencer'].split('/')
                self.set(f'{tone}/mod', int(mod), which='dict')
                self.set(f'{tone}/out', int(out), which='dict')
                self.set(f'{tone}/seq', int(seq), which='dict')

                # Set NCO frequency for each tone.
                mod_freq = self[f'{tone}/freq'] - self[f'lo_freq/M{mod}O{out}']
                assert -500*u.MHz < mod_freq < 500*u.MHz, f'Varman: mod_freq of {tone} is out of range.'
                self.set(f'{tone}/mod_freq', mod_freq, which='dict')
                
                # Make sure readout_levels are in ascending order.
                self.set(f'{tone}/readout_levels', sorted(self[f'{tone}/readout_levels']), which='dict')
                self.set(f'{tone}/lowest_readout_levels', self[f'{tone}/readout_levels'][0], which='dict')
                self.set(f'{tone}/highest_readout_levels', self[f'{tone}/readout_levels'][-1], which='dict')
                self.set(f'{tone}/n_readout_levels', len(self[f'{tone}/readout_levels']), which='dict')

            else:
                pass

        self.set('tones', tones_list, which='dict')


    def transmon_parameters(self, transmon: str, resonator: str, chi_kHz: float = None):
        """
        All return values are in [GHz].
        
        Calculate transmon property like EJ, EC, g, etc from values in variables.yaml.
        Notice the full dispersive shift/ac Stark shift is 2 * chi.
        Notice fr is a little bit tricky, but doesn't influence the result too much.
        """
        try:
            from qtrlb.utils.transmon_parameters3 import falpha_to_EJEC, get_bare_frequency
        except ModuleNotFoundError:
            print('Missing the module to run such function')
            return

        assert transmon.startswith('Q'), 'Transmon has to be string like "Q0", "Q1".'
        assert resonator.startswith('R'), 'Resonator has to be string like "R3", "R4a".'
        f01_GHz = self[f'{transmon}/01/freq'] / u.GHz
        alpha_GHz = self[f'{transmon}/12/anharmonicity'] / u.GHz
        fr_GHz = self[f'{resonator}/freq'] / u.GHz
        
        # The return values depends on whether user has run ReadoutFrequencyScan.
        if chi_kHz is None:
            EJ, EC = falpha_to_EJEC(f01_GHz, alpha_GHz)
            return EJ, EC
        else:
            chi_GHz = chi_kHz * u.kHz / u.GHz
            f01_b, alpha_b, fr_b, g01 = get_bare_frequency(f01_GHz, alpha_GHz, fr_GHz, chi_GHz)
            EJ, EC = falpha_to_EJEC(f01_b, alpha_b)
            return EJ, EC, g01

