import pytest
from regym.environments import parse_environment


@pytest.fixture
def ppo_config_dict():
    config = dict()
    config['discount'] = 0.99
    config['use_gae'] = False
    config['use_cuda'] = False
    config['gae_tau'] = 0.95
    config['entropy_weight'] = 0.01
    config['gradient_clip'] = 5
    config['optimization_epochs'] = 10
    config['mini_batch_size'] = 256
    config['ppo_ratio_clip'] = 0.2
    config['learning_rate'] = 3.0e-4
    config['adam_eps'] = 1.0e-5
    config['horizon'] = 1024
    config['phi_arch'] = 'MLP'
    config['actor_arch'] = 'None'
    config['critic_arch'] = 'None'
    return config


@pytest.fixture
def ppo_rnn_config_dict():
    config = dict()
    config['discount'] = 0.99
    config['use_gae'] = False
    config['use_cuda'] = False
    config['gae_tau'] = 0.95
    config['entropy_weight'] = 0.01
    config['gradient_clip'] = 5
    config['optimization_epochs'] = 10
    config['mini_batch_size'] = 32
    config['ppo_ratio_clip'] = 0.2
    config['learning_rate'] = 3.0e-4
    config['adam_eps'] = 1.0e-5
    config['horizon'] = 256
    config['phi_arch'] = 'RNN'
    config['actor_arch'] = 'None'
    config['critic_arch'] = 'None'
    return config


@pytest.fixture
def dqn_config_dict():
    config = dict()
    config['learning_rate'] = 1.0e-3
    config['epsstart'] = 1
    config['epsend'] = 0.1
    config['epsdecay'] = 5.0e4
    config['double'] = False
    config['dueling'] = False
    config['use_cuda'] = False
    config['use_PER'] = False
    config['PER_alpha'] = 0.07
    config['min_memory'] = 1.e03
    config['memoryCapacity'] = 1.e03
    config['nbrTrainIteration'] = 8
    config['batch_size'] = 256
    config['gamma'] = 0.99
    config['tau'] = 1.0e-2
    return config


@pytest.fixture
def ddpg_config_dict():
    config = dict()
    config['tau'] = 1e-4
    config['gamma'] = 0.99
    config['use_cuda'] = False
    config['nbrTrainIteration'] = 1
    config['action_scaler'] = 1.0
    config['use_HER'] = False
    config['HER_k'] = 2
    config['HER_strategy'] = 'future'
    config['HER_use_singlegoal'] = False
    config['use_PER'] = False
    config['PER_alpha'] = 0.7
    config['memory_capacity'] = 25e3
    config['min_capacity'] = 25e3
    config['batch_size'] = 512
    config['learning_rate'] = 3.0e-4
    return config


@pytest.fixture
def tabular_q_learning_config_dict():
    config = dict()
    config['learning_rate'] = 0.9
    config['discount_factor'] = 0.99
    config['epsilon_greedy'] = 0.1
    config['use_repeated_update_q_learning'] = False
    config['temperature'] = 1
    return config


@pytest.fixture
def reinforce_config_dict():
    config = dict()
    config['learning_rate'] = 1.0e-3
    config['episodes_before_update'] = 5 # Do not make less than 2, for reinforce_test.py
    config['adam_eps'] = 1.0e-5
    return config


@pytest.fixture
def a2c_config_dict():
    config = dict()
    config['discount_factor'] = 0.9
    config['k_steps'] = 5
    config['samples_before_update'] = 30
    config['learning_rate'] = 1.0e-3
    config['adam_eps'] = 1.0e-5
    return config


@pytest.fixture
def FrozenLakeTask(): # Discrete Action / Observation space
    return parse_environment('FrozenLake-v0')


@pytest.fixture
def CartPoleTask(): # Discrete Action / Continuous Observation space
    return parse_environment('CartPole-v0')


@pytest.fixture
def PendulumTask(): # Continuous Action / Observation space
    return parse_environment('Pendulum-v0')


@pytest.fixture
def RPSTask():
    return parse_environment('RockPaperScissors-v0')
