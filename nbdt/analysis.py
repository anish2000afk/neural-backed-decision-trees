from nbdt.utils import set_np_printoptions, Colors
from nbdt.graph import wnid_to_synset, synset_to_wnid
from nbdt.model import (
    SoftEmbeddedDecisionRules as SoftRules,
    HardEmbeddedDecisionRules as HardRules
)
from torch.distributions import Categorical
import torch.nn.functional as F
from collections import defaultdict
import torch
from nbdt import metrics
import functools
import numpy as np
import os
from PIL import Image
from pathlib import Path
import time


__all__ = names = (
    'Noop', 'ConfusionMatrix', 'ConfusionMatrixJointNodes',
    'IgnoredSamples', 'HardEmbeddedDecisionRules', 'SoftEmbeddedDecisionRules',
    'Entropy', 'NBDTEntropy', 'Superclass', 'SuperclassNBDT')
keys = ('path_graph', 'path_wnids', 'classes', 'dataset', 'metric',
        'dataset_test', 'superclass_wnids')


def add_arguments(parser):
    parser.add_argument('--superclass-wnids', nargs='*', type=str)


def start_end_decorator(obj, name):
    start = getattr(obj, f'start_{name}', None)
    end = getattr(obj, f'end_{name}', None)
    assert start and end
    def decorator(f):
        @functools.wraps(f)
        def wrapper(epoch, *args, **kwargs):
            start(epoch)
            f(epoch, *args, **kwargs)
            end(epoch)
        return wrapper
    return decorator


class StartEndContext:

    def __init__(self, obj, name, epoch=0):
        self.obj = obj
        self.name = name
        self.epoch = epoch

    def __call__(self, epoch):
        self.epoch = epoch
        return self

    def __enter__(self):
        return getattr(self.obj, f'start_{self.name}')(self.epoch)

    def __exit__(self, type, value, traceback):
        getattr(self.obj, f'end_{self.name}')(self.epoch)


class Noop:

    accepts_classes = lambda testset, **kwargs: testset.classes

    def __init__(self, classes=()):
        set_np_printoptions()

        self.classes = classes
        self.num_classes = len(classes)
        self.epoch = None

    @property
    def epoch_function(self):
        return start_end_decorator(self, 'epoch')

    @property
    def train_function(self):
        return start_end_decorator(self, 'train')

    @property
    def test_function(self):
        return start_end_decorator(self, 'test')

    @property
    def epoch_context(self):
        return StartEndContext(self, 'epoch')

    def start_epoch(self, epoch):
        self.epoch = epoch

    def start_train(self, epoch):
        assert epoch == self.epoch

    def update_batch(self, outputs, targets, images):
        self._update_batch(outputs, targets)

    def _update_batch(self, outputs, targets):
        pass

    def end_train(self, epoch):
        assert epoch == self.epoch

    def start_test(self, epoch):
        assert epoch == self.epoch

    def end_test(self, epoch):
        assert epoch == self.epoch

    def end_epoch(self, epoch):
        assert epoch == self.epoch


class ConfusionMatrix(Noop):

    def __init__(self, classes):
        super().__init__(classes)
        self.k = len(classes)
        self.m = None

    def start_train(self, epoch):
        super().start_train(epoch)
        raise NotImplementedError()

    def start_test(self, epoch):
        super().start_test(epoch)
        self.m = np.zeros((self.k, self.k))

    def _update_batch(self, outputs, targets):
        super()._update_batch(outputs, targets)
        _, predicted = outputs.max(1)
        if len(predicted.shape) == 1:
            predicted = predicted.numpy().ravel()
            targets = targets.numpy().ravel()
            ConfusionMatrix.update(self.m, predicted, targets)

    def end_test(self, epoch):
        super().end_test(epoch)
        recall = self.recall()
        for row, cls in zip(recall, self.classes):
            print(row, cls)
        print(recall.diagonal(), '(diagonal)')

    @staticmethod
    def update(confusion_matrix, preds, labels):
        preds = tuple(preds)
        labels = tuple(labels)

        for pred, label in zip(preds, labels):
            confusion_matrix[label, pred] += 1

    @staticmethod
    def normalize(confusion_matrix, axis):
        total = confusion_matrix.astype(np.float).sum(axis=axis)
        total = total[:, None] if axis == 1 else total[None]
        return confusion_matrix / total

    def recall(self):
        return ConfusionMatrix.normalize(self.m, 1)

    def precision(self):
        return ConfusionMatrix.normalize(self.m, 0)


class IgnoredSamples(Noop):
    """ Counter for number of ignored samples in decision tree """

    def __init__(self, classes=()):
        super().__init__(classes)
        self.ignored = None

    def start_test(self, epoch):
        super().start_test(epoch)
        self.ignored = 0

    def _update_batch(self, outputs, targets):
        super()._update_batch(outputs, targets)
        self.ignored += outputs[:,0].eq(-1).sum().item()
        return self.ignored

    def end_test(self, epoch):
        super().end_test(epoch)
        print("Ignored Samples: {}".format(self.ignored))


class DecisionRules(Noop):
    """Generic support for evaluating embedded decision rules."""

    accepts_dataset = lambda trainset, **kwargs: trainset.__class__.__name__
    accepts_path_graph = True
    accepts_path_wnids = True
    accepts_metric = True

    name = 'NBDT'

    def __init__(self, *args, Rules=HardRules, metric='top1', **kwargs):
        self.rules = Rules(*args, **kwargs)
        super().__init__(self.rules.tree.classes)
        self.metric = getattr(metrics, metric)()

    def start_test(self, epoch):
        self.metric.clear()

    def _update_batch(self, outputs, targets):
        super()._update_batch(outputs, targets)
        outputs = self.rules.forward(outputs)
        self.metric.forward(outputs, targets)
        accuracy = round(self.metric.correct / float(self.metric.total), 4) * 100
        return accuracy

    def end_test(self, epoch):
        super().end_test(epoch)
        accuracy = round(self.metric.correct / (self.metric.total or 1) * 100., 2)
        print(f'[{self.name}] Acc: {accuracy}%, {self.metric.correct}/{self.metric.total}')


class HardEmbeddedDecisionRules(DecisionRules):
    """Evaluation is hard."""

    name = 'NBDT-Hard'


class SoftEmbeddedDecisionRules(DecisionRules):
    """Evaluation is soft."""

    name = 'NBDT-Soft'

    def __init__(self, *args, Rules=None, **kwargs):
        super().__init__(*args, Rules=SoftRules, **kwargs)


class Entropy(Noop):

    def __init__(self, classes=(), k=20, path='out/entropy-{epoch}-{time}/image-{suffix}-{i}-{score:.2e}.jpg'):
        super().__init__(classes)
        self.reset()
        self.k = k
        self.path = Path(path)
        self.time = int(time.time())

    def start_test(self, epoch):
        super().start_test(epoch)
        self.reset()

    def reset(self):
        self.avg = 0
        self.std = 0
        self.max = []
        self.min = []
        self.i = 0

    def score(self, entropy ,outputs, images):
        return entropy

    @staticmethod
    def last(t):
        return t[-1]

    def update_batch(self, outputs, targets, images):
        super().update_batch(outputs, targets, images)

        probs = F.softmax(outputs, dim=1)
        entropies = list(Categorical(probs=probs).entropy().cpu().detach().numpy())
        for e_i in entropies:
            self.i += 1
            avg_i_minus_1 = self.avg
            self.avg = avg_i_minus_1 + ((e_i - avg_i_minus_1) / self.i)
            self.std = self.std + (e_i - avg_i_minus_1) * (e_i - self.avg)

        scores = self.score(entropies, outputs, images)
        eois = list(zip(entropies, outputs, images, scores))
        self.max = list(sorted(self.max + eois, reverse=True, key=Entropy.last))[:self.k]
        self.min = list(sorted(self.min + eois, key=Entropy.last))[:self.k]

    def end_test(self, epoch):
        super().end_test(epoch)
        print(f'[Entropy] avg {self.avg:.2e}, std {self.std:.2e}, max {self.max[0][0]:.2e}, min {self.min[0][0]:.2e}')

        directory = str(self.path.parent).format(time=self.time, epoch=self.epoch)
        os.makedirs(directory, exist_ok=True)
        for name, suffix, lst in (('highest', 'max', self.max), ('lowest', 'min', self.min)):
            print(f'==> Saving {self.k} {name} entropy images in {directory}')
            for i, (_, _, image, score) in enumerate(lst):
                Image.fromarray(
                    (image.permute(1, 2, 0) * 255).cpu().detach().numpy().astype(np.uint8)
                ).save(str(self.path).format(
                    epoch=self.epoch, i=i, suffix=suffix, score=score,
                    time=self.time))


class NBDTEntropy(Entropy):
    """Collect and log samples according to NBDT path entropy difference"""

    accepts_dataset = lambda trainset, **kwargs: trainset.__class__.__name__
    accepts_path_graph = True
    accepts_path_wnids = True

    def __init__(self, *args, Rules=HardRules, path_graph=None,
            path_wnids=None, dataset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rules = Rules(
            path_graph=path_graph, path_wnids=path_wnids, dataset=dataset)

    def score(self, entropy, outputs, images):
        decisions = self.rules.forward_with_decisions(outputs)
        entropies = [[node['entropy'] for node in path] for path in decisions[1]]
        return [max(ent) - min(ent) for ent in entropies]


class Superclass(DecisionRules):
    """Evaluate provided model on superclasses

    Each wnid must be a hypernym of at least one label in the test set.
    This metric will convert each predicted class into the corrresponding
    wnid and report accuracy on this len(wnids)-class problem.
    """

    accepts_dataset = lambda trainset, **kwargs: trainset.__class__.__name__
    accepts_dataset_test = lambda testset, **kwargs: testset.__class__.__name__
    name = 'Superclass'
    accepts_superclass_wnids = True

    def __init__(self, *args, superclass_wnids, dataset_test=None,
            Rules=SoftRules, metric=None, **kwargs):
        """Pass wnids to classify.

        Assumes index of each wnid is the index of the wnid in the rules.wnids
        list. This agrees with Node.wnid_to_class_index as of writing, since
        rules.wnids = get_wnids(...).
        """
        # TODO: for now, ignores metric
        super().__init__(*args, **kwargs)

        kwargs['dataset'] = dataset_test
        kwargs.pop('path_graph', '')
        kwargs.pop('path_wnids', '')
        self.rules_test = Rules(*args, **kwargs)
        self.superclass_wnids = superclass_wnids
        self.total = self.correct = 0

        self.mapping_target, self.new_to_old_classes_target = Superclass.build_mapping(self.rules_test.tree.wnids_leaves, superclass_wnids)
        self.mapping_pred, self.new_to_old_classes_pred = Superclass.build_mapping(self.rules.tree.wnids_leaves, superclass_wnids)

        mapped_classes = [self.classes[i] for i in (self.mapping_target >= 0).nonzero()]
        Colors.cyan(
            f'==> Mapped {len(mapped_classes)} classes to your superclasses: '
            f'{mapped_classes}')

    @staticmethod
    def build_mapping(dataset_wnids, superclass_wnids):
        new_to_old_classes = defaultdict(lambda: [])
        mapping = []
        for old_index, dataset_wnid in enumerate(dataset_wnids):
            synset = wnid_to_synset(dataset_wnid)
            hypernyms = Superclass.all_hypernyms(synset)
            hypernym_wnids = list(map(synset_to_wnid, hypernyms))

            value = -1
            for new_index, superclass_wnid in enumerate(superclass_wnids):
                if superclass_wnid in hypernym_wnids:
                    value = new_index
                    break
            mapping.append(value)
            new_to_old_classes[value].append(old_index)
        mapping = torch.Tensor(mapping)
        return mapping, new_to_old_classes

    @staticmethod
    def all_hypernyms(synset):
        hypernyms = []
        frontier = [synset]
        while frontier:
            current = frontier.pop(0)
            hypernyms.append(current)
            frontier.extend(current.hypernyms())
        return hypernyms

    def forward(self, outputs, targets):
        if self.mapping_target.device != targets.device:
            self.mapping_target = self.mapping_target.to(targets.device)

        if self.mapping_pred.device != outputs.device:
            self.mapping_pred = self.mapping_pred.to(outputs.device)

        targets = self.mapping_target[targets]
        outputs = outputs[targets >= 0]
        targets = targets[targets >= 0]

        outputs[:, self.mapping_pred < 0] = -100
        if outputs.size(0) == 0:
            return torch.Tensor([]), torch.Tensor([])
        predicted = outputs.max(1)[1]
        predicted = self.mapping_pred[predicted].to(targets.device)
        return predicted, targets

    def update_batch(self, outputs, targets):
        predicted, targets = self.forward(outputs, targets)

        n_samples = predicted.size(0)
        self.total += n_samples
        self.correct += (predicted == targets).sum().item()
        accuracy = round(self.correct / (float(self.total) or 1), 4) * 100
        return f'{self.name}: {accuracy}%'


class SuperclassNBDT(Superclass):

    name = 'Superclass-NBDT'

    def __init__(self, *args, Rules=None, **kwargs):
        super().__init__(*args, Rules=SoftRules, **kwargs)

    def forward(self, outputs, targets):
        outputs = self.rules.get_node_logits(
            outputs,
            new_to_old_classes=self.new_to_old_classes_pred,
            num_classes=max(self.new_to_old_classes_pred) + 1)
        predicted = outputs.max(1)[1].to(targets.device)

        if self.mapping_target.device != targets.device:
            self.mapping_target = self.mapping_target.to(targets.device)

        targets = self.mapping_target[targets]
        predicted = predicted[targets >= 0]
        targets = targets[targets >= 0]
        return predicted, targets
