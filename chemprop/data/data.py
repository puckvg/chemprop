from argparse import Namespace
from collections import defaultdict
from logging import Logger
import math
from multiprocessing import Pool
import random
from typing import Callable, Dict, List, Tuple, Union, Set, FrozenSet

import numpy as np
from torch.utils.data.dataset import Dataset
from rdkit import Chem
from tqdm import tqdm

from .scaler import StandardScaler
from .vocab import load_vocab, Vocab, get_substructures, substructure_to_feature
from chemprop.features import get_features_func, get_kernel_func


class SparseNoneArray:
    def __init__(self, targets: List[float]):
        self.length = len(targets)
        self.targets = defaultdict(lambda: None, {i: x for i, x in enumerate(targets) if x is not None})
    
    def __len__(self):
        return self.length
    
    def __getitem__(self, i):
        if i >= self.length:
            raise IndexError
        return self.targets[i]


class MoleculeDatapoint:
    def __init__(self,
                 line: List[str],
                 args: Namespace = None,
                 features: np.ndarray = None,
                 use_compound_names: bool = False):
        """
        Initializes a MoleculeDatapoint.

        :param line: A list of strings generated by separating a line in a data CSV file by comma.
        :param args: Argument Namespace.
        :param features: A numpy array containing additional features (ex. Morgan fingerprint).
        :param use_compound_names: Whether the data CSV includes the compound name on each line.
        """
        if args is not None:
            self.features_generator, self.predict_features, self.sparse = args.features_generator, args.predict_features, args.sparse
            self.predict_features_and_task = args.predict_features_and_task
            self.bert_pretraining = args.dataset_type == 'bert_pretraining'
            self.bert_mask_prob = args.bert_mask_prob
            self.bert_mask_type = args.bert_mask_type
            self.bert_vocab_func = args.bert_vocab_func
            self.substructure_sizes = args.bert_substructure_sizes

            self.kernel = args.dataset_type == 'kernel'
            self.kernel_func = args.kernel_func

            self.maml = args.maml

            self.args = args
        else:
            self.features_generator = self.bert_mask_prob = self.bert_mask_type = self.bert_vocab_func = self.substructure_sizes = self.args = self.kernel = self.kernel_func = None
            self.predict_features_and_task = self.predict_features = self.sparse = self.bert_pretraining = self.maml = False

        if features is not None and self.features_generator is not None:
            raise ValueError('Currently cannot provide both loaded features and a features generator.')

        self.features = features

        if use_compound_names:
            self.compound_name = line[0]  # str
            line = line[1:]
        else:
            self.compound_name = None

        self.smiles = line[0]  # str
        self.mol = Chem.MolFromSmiles(self.smiles)

        # Generate additional features if given a generator
        if self.features_generator is not None:
            self.features = []

            for fg in self.features_generator:
                features_func = get_features_func(fg, args)
                self.features.extend(features_func(self.mol))

            self.features = np.array(self.features)

        # Fix nans in features
        if self.features is not None:
            replace_token = None if self.predict_features else 0
            self.features = np.where(np.isnan(self.features), replace_token, self.features)

        # Create targets
        self.task_targets = [float(x) if x != '' else None for x in line[1:]]
        self.recreate_targets()

    def set_features(self, features: np.ndarray):
        self.features = features
        self.recreate_targets()
    
    def num_tasks(self):
        if self.args is not None and self.args.dataset_type in ['unsupervised', 'bert_pretraining', 'kernel']:
            return 1  # TODO could try doing "multitask" with multiple different clusters?
        else:
            return len(self.targets) if self.targets is not None else 1

    def recreate_targets(self):
        if self.args is not None and self.args.dataset_type in ['unsupervised', 'bert_pretraining', 'kernel']:
            self.targets = None
        elif self.predict_features_and_task:
            self.targets = np.concatenate([np.array(self.task_targets), self.features])
        elif self.predict_features:
            self.targets = self.features
        else:
            self.targets = self.task_targets

        if self.sparse:
            self.targets = SparseNoneArray(self.targets)

    def bert_init(self):
        if not self.bert_pretraining:
            raise Exception('Should not do this unless using bert_pretraining.')

        self.vocab_targets, self.nb_indices = self.args.vocab.smiles2indices(self.smiles)
        self.recreate_mask()

    def recreate_mask(self):
        # Note: 0s to mask atoms which should be predicted

        if not self.bert_pretraining:
            raise Exception('Cannot recreate mask without bert_pretraining on.')
        
        if self.bert_vocab_func == 'substructure':
            self.substructures = get_substructures(list(self.mol.GetAtoms()), 
                                                   sizes=self.substructure_sizes, 
                                                   max_count=1)  # TODO could change max_count
            self.substructure_index_map = substructure_index_mapping(self.mol, self.substructures)
            self.mask = np.ones(max(self.substructure_index_map) + 1)
            self.mask[-len(self.substructures):] = 0  # the last entries correspond to the substructures
            self.mask = list(self.mask)

            sorted_substructures = sorted(list(self.substructures), key=lambda x: self.substructure_index_map[list(x)[0]])
            substructure_index_labels = \
                    [self.args.vocab.w2i(substructure_to_feature(self.mol, substruct)) for substruct in sorted_substructures]
            self.vocab_targets = np.zeros(len(self.mask))  # these should never get used
            if len(substructure_index_labels) > 0:  # it's possible to find none at all in e.g. a 2-atom molecule
                self.vocab_targets[-len(self.substructures):] = np.array(substructure_index_labels)
            return

        num_targets = len(self.vocab_targets)

        if self.bert_mask_type == 'cluster':
            self.mask = np.ones(num_targets)
            atoms = set(range(num_targets))
            while len(atoms) != 0:
                atom = atoms.pop()
                neighbors = self.nb_indices[atom]
                cluster = [atom] + neighbors

                # note: divide by cluster size to preserve overall probability of masking each atom
                if np.random.random() < self.bert_mask_prob / len(cluster):
                    self.mask[cluster] = 0
                    atoms -= set(neighbors)

            # Ensure at least one cluster of 0s
            if sum(self.mask) == len(self.mask):
                atom = np.random.randint(len(self.mask))
                neighbors = self.nb_indices[atom]
                cluster = [atom] + neighbors
                self.mask[cluster] = 0

        elif self.bert_mask_type == 'correlation':
            self.mask = np.random.rand(num_targets) > self.bert_mask_prob  # len = num_atoms

            # randomly change parts of mask to increase correlation between neighbors
            for _ in range(len(self.mask)):  # arbitrary num iterations; could set in parsing if we want
                index_to_change = random.randint(0, len(self.mask) - 1)
                if len(self.nb_indices[index_to_change]) > 0:  # can be 0 for single heavy atom molecules
                    nbr_index = random.randint(0, len(self.nb_indices[index_to_change]) - 1)
                    self.mask[index_to_change] = self.mask[nbr_index]

            # Ensure at least one 0 so at least one thing is predicted
            if sum(self.mask) == len(self.mask):
                self.mask[np.random.randint(len(self.mask))] = 0

        elif self.bert_mask_type == 'random':
            self.mask = np.random.rand(num_targets) > self.bert_mask_prob  # len = num_atoms

            # Ensure at least one 0 so at least one thing is predicted
            if sum(self.mask) == len(self.mask):
                self.mask[np.random.randint(len(self.mask))] = 0

        else:
            raise ValueError(f'bert_mask_type "{self.bert_mask_type}" not supported.')

        # np.ndarray --> list
        self.mask = list(self.mask)

    def set_targets(self, targets: List[float]):  # for unsupervised pretraining only
        self.targets = targets


def bert_init(d: MoleculeDatapoint) -> MoleculeDatapoint:
    d.bert_init()
    return d


class MoleculeDataset(Dataset):
    def __init__(self, data: List[MoleculeDatapoint]):
        self.data = data
        self.bert_pretraining = self.data[0].bert_pretraining if len(self.data) > 0 else False
        self.bert_vocab_func = self.data[0].bert_vocab_func if len(self.data) > 0 else None
        self.kernel = self.data[0].kernel if len(self.data) > 0 else False
        if self.kernel:
            # want an even number of data points
            if len(self.data) % 2 == 1:
                self.data = self.data[:-1]
        self.kernel_func = get_kernel_func(self.data[0].kernel_func) if len(self.data) > 0 and self.data[0].kernel_func is not None else None
        self.maml = self.data[0].maml if len(self.data) > 0 else False
        self.args = self.data[0].args if len(self.data) > 0 else None
        self.scaler = None
    
    def bert_init(self, args: Namespace, logger: Logger = None):
        debug = logger.debug if logger is not None else print

        if not hasattr(args, 'vocab'):
            debug('Determining vocab')
            args.vocab = load_vocab(args.checkpoint_paths[0]) if args.checkpoint_paths is not None else Vocab(args, self.smiles())
            debug(f'Vocab/Output size = {args.vocab.output_size:,}')

        if args.sequential:
            for d in tqdm(self.data, total=len(self.data)):
                d.bert_init()
        else:
            try:
                # reassign self.data since the pool seems to deepcopy the data before calling bert_init
                with Pool() as pool:
                    self.data = pool.map(bert_init, self.data)

            except OSError:  # apparently it's possible to get an OSError about too many open files here...?
                for d in self.data:
                    d.bert_init()

        debug('Finished initializing targets and masks for bert')

    def maml_init(self, task_indices: List[int]):
        """Limits targets to those tasks which are specified and determines which tasks are known for which moleecules."""
        # Eliminate targets that are not in task_indices because they belong to a different meta split
        task_indices = set(task_indices)
        for d in self.data:
            d.targets = [t for i, t in enumerate(d.targets) if i in task_indices]

        # Determine which tasks are known for which molecules and place in self.has_target_indices
        targets = self.targets()
        if len(targets) > 0 and targets[0] is not None:
            self.has_target_indices = [[] for _ in range(self.num_tasks())]
            for i, t in enumerate(targets):
                for j, label in enumerate(t):
                    if label is not None:
                        self.has_target_indices[j].append(i)

    def sample_maml_task(self, args: Namespace, seed: int = None) -> Tuple['MoleculeDataset', 'MoleculeDataset', int]:
        """
        Samples a task for maml depending on the split. Returns train and test data for that task.

        :param args: Arguments.
        :param seed: Random seed for shuffling train and test data.
        :return: A train MoleculeDataset and a test MoleculeDataset for a sampled task and the task index.
        """
        task_idx = random.randint(0, self.num_tasks() - 1)

        data_idx_with_label = self.has_target_indices[task_idx]
        half = len(data_idx_with_label) // 2
        task_train_data, task_test_data = data_idx_with_label[:half], data_idx_with_label[half:]

        if seed is not None:
            random.seed(seed)
        random.shuffle(task_train_data)
        random.shuffle(task_test_data)

        size = args.batch_size // 2
        task_train_data, task_test_data = \
                    MoleculeDataset([self.data[i] for i in task_train_data[:size]]), \
                    MoleculeDataset([self.data[i] for i in task_test_data[:size]])

        return task_train_data, task_test_data, task_idx

    def compound_names(self) -> List[str]:
        if len(self.data) == 0 or self.data[0].compound_name is None:
            return None

        return [d.compound_name for d in self.data]

    def smiles(self) -> List[str]:
        if self.args is not None and hasattr(self.data[0], 'substructures'):
            return [(d.smiles, d.substructure_index_map, d.substructures) for d in self.data]
        return [d.smiles for d in self.data]
    
    def mols(self) -> List[str]:
        if hasattr(self.data[0], 'substructures'):
            return [(d.mol, d.substructure_index_map, d.substructures) for d in self.data]
        return [d.mol for d in self.data]

    def pairs(self) -> List[Tuple[MoleculeDatapoint, MoleculeDatapoint]]:
        paired_data = []
        for i in range(0, len(self.data), 2):
            paired_data.append((self.data[i], self.data[i+1]))
        return paired_data

    def features(self) -> List[np.ndarray]:
        if len(self.data) == 0 or self.data[0].features is None:
            return None

        return [d.features for d in self.data]

    def targets(self, task_idx: int = None) -> Union[List[List[float]],
                               List[SparseNoneArray],
                               List[int],
                               Dict[str, Union[List[np.ndarray], List[int]]]]:
        if self.bert_pretraining:
            return {
                'features': self.features(),
                'vocab': [word for d in self.data for word in d.vocab_targets]
            }

        if self.kernel:
            return [[self.kernel_func(*pair)] for pair in self.pairs()]

        targets = [d.targets for d in self.data]

        if task_idx is not None:
            targets = [t[task_idx] for t in targets]

        return targets

    def num_tasks(self) -> int:
        return self.data[0].num_tasks() if len(self.data) > 0 else None

    def features_size(self) -> int:
        return len(self.data[0].features) if len(self.data) > 0 and self.data[0].features is not None else None

    def mask(self) -> List[int]:
        if not self.bert_pretraining:
            raise Exception('Mask is undefined without bert_pretraining on.')

        return [m for d in self.data for m in d.mask]

    def shuffle(self, seed: int = None):
        if self.maml:
            return  # shuffling is done in sample_maml_task

        if seed is not None:
            random.seed(seed)

        random.shuffle(self.data)

        if self.bert_pretraining:
            for d in self.data:
                d.recreate_mask()

    def chunk(self, num_chunks: int, seed: int = None) -> List['MoleculeDataset']:
        self.shuffle(seed)
        datasets = []
        chunk_len = math.ceil(len(self.data) / num_chunks)
        for i in range(num_chunks):
            datasets.append(MoleculeDataset(self.data[i * chunk_len:(i + 1) * chunk_len]))

        return datasets
    
    def normalize_features(self, scaler: StandardScaler = None, replace_nan_token: int = 0) -> StandardScaler:
        if len(self.data) == 0 or self.data[0].features is None:
            return None

        if scaler is not None:
            self.scaler = scaler

        elif self.scaler is None:
            features = np.vstack([d.features for d in self.data])
            self.scaler = StandardScaler(replace_nan_token=replace_nan_token)
            self.scaler.fit(features)

        for d in self.data:
            d.set_features(self.scaler.transform(d.features.reshape(1, -1))[0])

        return self.scaler
    
    def set_targets(self, targets: List[List[float]]):  # for unsupervised pretraining only
        assert len(self.data) == len(targets) # assume user kept them aligned
        for i in range(len(self.data)):
            self.data[i].set_targets(targets[i])

    def sort(self, key: Callable):
        self.data.sort(key=key)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, item) -> Union[MoleculeDatapoint, List[MoleculeDatapoint]]:
        return self.data[item]


def substructure_index_mapping(smiles: str, substructures: Set[FrozenSet[int]]) -> List[int]:
    """
    Return a deterministic mapping of indices from the original molecule atoms to the
    molecule with some substructures collapsed.

    :param smiles: smiles string
    :param substructures: indices of atoms in substructures
    """
    if type(smiles) == str:
        mol = Chem.MolFromSmiles(smiles)
    else:
        mol = smiles
    num_atoms = mol.GetNumAtoms()
    atoms_in_substructures = set().union(*substructures)  # set of all indices of atoms in a substructure
    remaining_atoms = set(range(num_atoms)) - atoms_in_substructures

    # collapsed substructures shouldn't share atoms
    assert sum(len(s) for s in substructures) == len(atoms_in_substructures)

    substructures = [sorted(list(substruct)) for substruct in substructures]
    substructures = sorted(substructures, key=lambda substruct: substruct[0])  # should give unique ordering b/c no shared atoms

    index_map = [None for _ in range(num_atoms)]
    remaining_atom_ordering = sorted(list(remaining_atoms))
    for i in range(len(remaining_atom_ordering)):
        index_map[remaining_atom_ordering[i]] = i
    for i in range(len(substructures)):
        for atom in substructures[i]:
            index_map[atom] = len(remaining_atom_ordering) + i
    
    return index_map
