# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

import copy
import logging
from operator import attrgetter
from typing import List, Optional, Tuple, Set

import mxnet as mx
import numpy as np

logger = logging.getLogger(__name__)

# Represents a list of raw constraints for a sentence. Each constraint is a list of target-word IDs.
RawConstraintList = List[List[int]]


class AvoidPhrase:
    """
    Represents a negative phrasal constraint. Tracks progress through a phrase
    returns the word ID that would complete the phrase in the avoid() method, allowing
    it to be avoided.

    :param avoid_list: A sequence of integers in the target language vocabulary.
    :param last_consumed: The last consumed word in the current hypothesis.
    """
    def __init__(self,
                 avoid_list: List[int],
                 last_consumed: int = -1) -> None:
        self.phrase = avoid_list
        self.last_consumed = last_consumed

    def __str__(self):
        return ' '.join(['*{}*'.format(x) if i == self.last_consumed else str(x) for i, x in enumerate(self.phrase)])

    def consume(self, word_id: int) -> 'AvoidPhrase':
        """
        Consumes a word, and updates the tracking based on it. If the word is the next words in the phrase,
        update last_consumed, otherwise, reset.

        :param word_id: The word that was just generated.
        """
        if self.phrase[self.last_consumed + 1] == word_id:
            return AvoidPhrase(self.phrase, self.last_consumed + 1)
        elif self.last_consumed != -1:
            return AvoidPhrase(self.phrase, self.last_consumed)
        else:
            return self

    def avoid(self) -> int:
        """
        Returns a word ID that should be avoided.

        :return: An integer representing a word that must not be generated next by this hypothesis.
        """
        if self.last_consumed == len(self.phrase) - 2:
            return self.phrase[-1]
        else:
            return 0


class AvoidBatch:
    """
    Represents a set of phrasal constraints for all items in the batch.
    For each hypotheses, there is a list of AvoidPhrase objects.

    The offset is used to return actual positions in the one-dimensionally-resized array that
    get set to infinity.

    :param avoid_list: The list of lists (one for each item in the batch).
    :param beam_size: The beam size.
    :param max_id: The maximum ID in the target vocabulary.
    """
    def __init__(self,
                 avoid_list: List[RawConstraintList],
                 beam_size: int,
                 max_id: int) -> None:
        self.avoid_list = []  # type: List[List[AvoidPhrase]]
        for raw_phrases in avoid_list:
            self.avoid_list += [[AvoidPhrase(phrase) for phrase in raw_phrases] * beam_size]
        self.offset = max_id

    def reorder(self, indices: mx.nd.array) -> None:
        """
        Reorders the avoid list according to the selected row indices.
        This can produce duplicates, but this is fixed in consume().

        :param indices: An mx.nd.array containing indices of hypotheses to select.
        """
        self.avoid_list = [self.avoid_list[x] for x in indices.asnumpy()]

    def consume(self, word_ids: mx.nd.array) -> None:
        """
        Takes an array representing the set of target word indices chosen for each hypothesis in the batch,
        and updates each of the negative constraints.

        :param word_ids: The set of word IDs.
        """
        word_ids = word_ids.asnumpy().tolist()
        for i, word_id in enumerate(word_ids):
            self.avoid_list[i] = [item.consume(word_ids[i]) for item in self.avoid_list[i]]

    def avoid(self) -> List[int]:
        """
        Returns a list of indices in a 1-dimensional reshaping of the scores array that should be set to null.

        :return: A list of indices.
        """
        to_avoid = []
        for i, items in enumerate(self.avoid_list):
            for phrase in items:
                word_id = phrase.avoid()
                if word_id > 0:
                    to_avoid.append((self.offset * i) + word_id)

        return to_avoid

class ConstrainedHypothesis:
    """
    Represents a set of words and phrases that must appear in the output.
    A constraint is of two types: sequence or non-sequence.
    A non-sequence constraint is a single word and can therefore be followed by anything,
    whereas a sequence constraint must be followed by a particular word (the next word in the sequence).
    This class also records which constraints have been met.

    A list of raw constraints is maintained internally as two parallel arrays. The following raw constraint
    represents two phrases that must appear in the output: 14 and 19 35 14.

        raw constraint: [[14], [19, 35, 14]]

    This is represented internally as:

        constraints: [14 19 35 14]
        is_sequence: [ 1  1  0  0]

    :param constraint_list: A list of zero or raw constraints (each represented as a list of integers).
    :param avoid_list: A list of zero or raw constraints that must *not* appear in the output.
    :param eos_id: The end-of-sentence ID.
    """

    def __init__(self,
                 constraint_list: RawConstraintList,
                 eos_id: int) -> None:

        # `constraints` records the words of the constraints, as a list (duplicates allowed).
        # `is_sequence` is a parallel array that records, for each corresponding constraint,
        #    whether the current word is the non-final word of a phrasal constraint.
        self.constraints = []  # type: List[int]
        self.is_sequence = []  # type: List[bool]
        for phrase in constraint_list:
            self.constraints += phrase
            self.is_sequence += [True] * len(phrase)
            self.is_sequence[-1] = False

        self.eos_id = eos_id

        # no constraints have been met
        self.met = [False for x in self.constraints]
        self.last_met = -1

    def __len__(self) -> int:
        """
        :return: The number of constraints.
        """
        return len(self.constraints)

    def __str__(self) -> str:
        s = []
        for i, word_id in enumerate(self.constraints):
            s.append(str(word_id) if self.met[i] is False else 'X')
            if self.is_sequence[i]:
                s.append('->')
        return ' '.join(s)

    def size(self) -> int:
        """
        :return: the number of constraints
        """
        return len(self.constraints)

    def num_met(self) -> int:
        """
        :return: the number of constraints that have been met.
        """
        return sum(self.met)

    def num_needed(self) -> int:
        """
        :return: the number of un-met constraints.
        """
        return self.size() - self.num_met()

    def allowed(self) -> Set[int]:
        """
        Returns the set of constrained words that could follow this one.
        For unfinished phrasal constraints, it is the next word in the phrase.
        In other cases, it is the list of all unmet constraints.
        If all constraints are met, an empty set is returned.

        :return: The ID of the next required word, or -1 if any word can follow
        """
        items = set()  # type: Set[int]
        # Add extensions of a started-but-incomplete sequential constraint
        if self.last_met != -1 and self.is_sequence[self.last_met] == 1:
            word_id = self.constraints[self.last_met + 1]
            if word_id != self.eos_id or self.num_needed() == 1:
                items.add(word_id)

        # Add all constraints that aren't non-initial sequences
        else:
            for i, word_id in enumerate(self.constraints):
                if not self.met[i] and (i == 0 or not self.is_sequence[i - 1]):
                    if word_id != self.eos_id or self.num_needed() == 1:
                        items.add(word_id)

        return items

    def finished(self) -> bool:
        """
        Return true if all the constraints have been met.

        :return: True if all the constraints are met.
        """
        return self.num_needed() == 0

    def is_valid(self, wordid) -> bool:
        """
        Ensures </s> is only generated when the hypothesis is completed.

        :param wordid: The wordid to validate.
        :return: True if all constraints are already met or the word ID is not the EOS id.
        """
        return self.finished() or wordid != self.eos_id or (self.num_needed() == 1 and self.eos_id in self.allowed())

    def advance(self, word_id: int) -> 'ConstrainedHypothesis':
        """
        Updates the constraints object based on advancing on word_id.
        There is a complication, in that we may have started but not
        yet completed a multi-word constraint.  We need to allow constraints
        to be added as unconstrained words, so if the next word is
        invalid, we must "back out" of the current (incomplete) phrase,
        re-setting all of its words as unmet.

        :param word_id: The word ID to advance on.
        :return: A deep copy of the object, advanced on word_id.
        """

        obj = copy.deepcopy(self)

        # First, check if we're updating a sequential constraint.
        if obj.last_met != -1 and obj.is_sequence[obj.last_met] == 1:
            if word_id == obj.constraints[obj.last_met + 1]:
                # Here, the word matches what we expect next in the constraint, so we update everything
                obj.met[obj.last_met + 1] = True
                obj.last_met += 1
            else:
                # Here, the word is not the expected next word of the constraint, so we back out of the constraint.
                index = obj.last_met
                while obj.is_sequence[index]:
                    obj.met[index] = False
                    index -= 1
                obj.last_met = -1

        # If not, check whether we're meeting a single-word constraint
        else:
            # Build a list from all constraints of tuples of the
            # form (constraint, whether it's a non-initial sequential, whether it's been met)
            constraint_tuples = list(zip(obj.constraints, [False] + obj.is_sequence[:-1], obj.met))
            # We are searching for an unmet constraint (word_id) that is not the middle of a phrase and is not met
            query = (word_id, False, False)
            try:
                pos = constraint_tuples.index(query)
                obj.met[pos] = True
                obj.last_met = pos
            except ValueError:
                # query not found; identical but duplicated object will be returned
                pass

        return obj


def init_batch(raw_constraints: List[Optional[RawConstraintList]],
               beam_size: int,
               start_id: int,
               eos_id: int) -> List[Optional[ConstrainedHypothesis]]:
    """
    :param raw_constraints: The list of raw constraints (list of list of IDs).
    :param beam_size: The beam size.
    :param start_id: The target-language vocabulary ID of the SOS symbol.
    :param eos_id: The target-language vocabulary ID of the EOS symbol.
    :return: A list of ConstrainedHypothesis objects (shape: (batch_size * beam_size,)).
    """
    constraints = [None] * (len(raw_constraints) * beam_size)  # type: List[Optional[ConstrainedHypothesis]]
    if any(raw_constraints):
        for i, raw_list in enumerate(raw_constraints):
            num_constraints = sum([len(phrase) for phrase in raw_list]) if raw_list is not None else 0
            if num_constraints > 0:
                hyp = ConstrainedHypothesis(raw_list, eos_id)
                idx = i * beam_size
                constraints[idx:idx + beam_size] = [hyp.advance(start_id) for x in range(beam_size)]

    return constraints


def get_bank_sizes(num_constraints: int,
                   beam_size: int,
                   candidate_counts: List[int]) -> List[int]:
    """
    Evenly distributes the beam across the banks, where each bank is a portion of the beam devoted
    to hypotheses having met the same number of constraints, 0..num_constraints.
    After the assignment, banks with more slots than candidates are adjusted.

    :param num_constraints: The number of constraints.
    :param beam_size: The beam size.
    :param candidate_counts: The empirical counts of number of candidates in each bank.
    :return: A distribution over banks.
    """

    num_banks = num_constraints + 1
    bank_size = beam_size // num_banks
    remainder = beam_size - bank_size * num_banks

    # Distribute any remainder to the end
    assigned = [bank_size for x in range(num_banks)]
    assigned[-1] += remainder

    # Now, moving right to left, push extra allocation to earlier buckets.
    # This encodes a bias for higher buckets, but if no candidates are found, space
    # will be made in lower buckets. This may not be the best strategy, but it is important
    # that you start pushing from the bucket that is assigned the remainder, for cases where
    # num_constraints >= beam_size.
    for i in reversed(range(num_banks)):
        overfill = assigned[i] - candidate_counts[i]
        if overfill > 0:
            assigned[i] -= overfill
            assigned[(i - 1) % num_banks] += overfill

    return assigned


class ConstrainedCandidate:
    """
    Object used to hold candidates for the beam in topk().

    :param row: The row in the scores matrix.
    :param col: The column (word ID) in the scores matrix.
    :param score: the associated accumulated score.
    :param hypothesis: The ConstrainedHypothesis containing information about met constraints.
    """

    __slots__ = ('row', 'col', 'score', 'hypothesis')

    def __init__(self,
                 row: int,
                 col: int,
                 score: float,
                 hypothesis: ConstrainedHypothesis) -> None:
        self.row = row
        self.col = col
        self.score = score
        self.hypothesis = hypothesis

    def __hash__(self):
        return hash((self.row, self.col))

    def __eq__(self, other):
        return self.row == other.row and self.col == other.col

    def __str__(self):
        return '({}, {}, {}, {})'.format(self.row, self.col, self.score, self.hypothesis.num_met())


def topk(batch_size: int,
         beam_size: int,
         inactive: mx.ndarray,
         scores: mx.ndarray,
         hypotheses: List[ConstrainedHypothesis],
         best_ids: mx.ndarray,
         best_word_ids: mx.ndarray,
         seq_scores: mx.ndarray,
         context: mx.context.Context) -> Tuple[np.array, np.array, np.array, List[ConstrainedHypothesis], mx.nd.array]:
    """
    Builds a new topk list such that the beam contains hypotheses having completed different numbers of constraints.
    These items are built from three different types: (1) the best items across the whole
    scores matrix, (2) the set of words that must follow existing constraints, and (3) k-best items from each row.

    :param batch_size: The number of segments in the batch.
    :param beam_size: The length of the beam for each segment.
    :param inactive: Array listing inactive rows (shape: (beam_size,)).
    :param scores: The scores array (shape: (beam_size, target_vocab_size)).
    :param hypotheses: The list of hypothesis objects.
    :param best_ids: The current list of best hypotheses (shape: (beam_size,)).
    :param best_word_ids: The parallel list of best word IDs (shape: (beam_size,)).
    :param seq_scores: (shape: (beam_size, 1)).
    :param context: The MXNet device context.
    :return: A tuple containing the best hypothesis rows, the best hypothesis words, the scores,
        the updated constrained hypotheses, and the updated set of inactive hypotheses.
    """

    for sentno in range(batch_size):
        rows = slice(sentno * beam_size, (sentno + 1) * beam_size)
        if hypotheses[rows.start] is not None and hypotheses[rows.start].size() > 0:
            best_ids[rows], best_word_ids[rows], seq_scores[rows], \
            hypotheses[rows], inactive[rows] = _topk(beam_size,
                                                     inactive[rows],
                                                     scores[rows],
                                                     hypotheses[rows],
                                                     best_ids[rows] - rows.start,
                                                     best_word_ids[rows],
                                                     seq_scores[rows],
                                                     context)

            # offsetting since the returned smallest_k() indices were slice-relative
            best_ids[rows] += rows.start
        else:
            # If there are no constraints for this sentence in the batch, everything stays
            # the same, except we need to mark all hypotheses as active
            inactive[rows] = 0

    return (best_ids, best_word_ids, seq_scores, hypotheses, inactive)


def _topk(beam_size: int,
          inactive: mx.ndarray,
          scores: mx.ndarray,
          hypotheses: List[ConstrainedHypothesis],
          best_ids: mx.ndarray,
          best_word_ids: mx.ndarray,
          sequence_scores: mx.ndarray,
          context: mx.context.Context) -> Tuple[np.array, np.array, np.array, List[ConstrainedHypothesis], mx.nd.array]:
    """
    Builds a new topk list such that the beam contains hypotheses having completed different numbers of constraints.
    These items are built from three different types: (1) the best items across the whole
    scores matrix, (2) the set of words that must follow existing constraints, and (3) k-best items from each row.

    :param beam_size: The length of the beam for each segment.
    :param inactive: Array listing inactive rows (shape: (beam_size,)).
    :param scores: The scores array (shape: (beam_size, target_vocab_size)).
    :param hypotheses: The list of hypothesis objects.
    :param best_ids: The current list of best hypotheses (shape: (beam_size,)).
    :param best_word_ids: The parallel list of best word IDs (shape: (beam_size,)).
    :param sequence_scores: (shape: (beam_size, 1)).
    :param context: The MXNet device context.
    :return: A tuple containing the best hypothesis rows, the best hypothesis words, the scores,
        the updated constrained hypotheses, and the updated set of inactive hypotheses.
    """

    num_constraints = hypotheses[0].size()

    candidates = set()
    # (1) Add all of the top-k items (which were passed) in as long as they pass the constraints
    for row, col, seq_score in zip(best_ids, best_word_ids, sequence_scores):
        row = int(row.asscalar())
        col = int(col.asscalar())
        if hypotheses[row].is_valid(col):
            seq_score = float(seq_score.asscalar())
            new_item = hypotheses[row].advance(col)
            cand = ConstrainedCandidate(row, col, seq_score, new_item)
            candidates.add(cand)

    # For each hypothesis, we add (2) all the constraints that could follow it and
    # (3) the best item (constrained or not) in that row
    best_next = mx.ndarray.argmin(scores, axis=1)
    for row in range(beam_size):
        if inactive[row]:
            continue

        hyp = hypotheses[row]

        # (2) add all the constraints that could extend this
        nextones = hyp.allowed()

        # (3) add the single-best item after this (if it's valid)
        col = int(best_next[row].asscalar())
        if hyp.is_valid(col):
            nextones.add(col)

        # Now, create new candidates for each of these items
        for col in nextones:
            new_item = hyp.advance(col)
            score = scores[row, col].asscalar()
            cand = ConstrainedCandidate(row, col, score, new_item)
            candidates.add(cand)

    # Sort the candidates. After allocating the beam across the banks, we will pick the top items
    # for each bank from this list
    sorted_candidates = sorted(candidates, key=attrgetter('score'))

    # The number of hypotheses in each bank
    counts = [0 for x in range(num_constraints + 1)]
    for cand in sorted_candidates:
        counts[cand.hypothesis.num_met()] += 1

    # Adjust allocated bank sizes if there are too few candidates in any of them
    bank_sizes = get_bank_sizes(num_constraints, beam_size, counts)

    # Sort the candidates into the allocated banks
    pruned_candidates = []  # type: List[ConstrainedCandidate]
    for i, cand in enumerate(sorted_candidates):
        bank = cand.hypothesis.num_met()

        if bank_sizes[bank] > 0:
            pruned_candidates.append(cand)
            bank_sizes[bank] -= 1

    inactive[:len(pruned_candidates)] = 0

    # Pad the beam so array assignment still works
    if len(pruned_candidates) < beam_size:
        inactive[len(pruned_candidates):] = 1
        pruned_candidates += [pruned_candidates[len(pruned_candidates) - 1]] * (beam_size - len(pruned_candidates))

    return (np.array([x.row for x in pruned_candidates]),
            np.array([x.col for x in pruned_candidates]),
            np.array([[x.score] for x in pruned_candidates]),
            [x.hypothesis for x in pruned_candidates],
            inactive)


def main(args):
    """
    Usage: python3 -m sockeye.lexical_constraints [--bpe BPE_MODEL]

    Reads sentences and constraints on STDIN (tab-delimited) and generates the JSON format
    that can be used when passing `--json-input` to sockeye.translate. It supports both positive
    constraints (phrases that must appear in the output) and negative constraints (phrases that
    must *not* appear in the output).

    e.g.,

        echo -e "Das ist ein Test .\tThis is\ttest" | python3 -m sockeye.lexical_constraints

    will produce the following JSON object:

        { "text": "Das ist ein Test .", "constraints": ["This is", "test"] }

    If you pass `--avoid` to the script, the constraints will be generated as negative constraints, instead:

        echo -e "Das ist ein Test .\tThis is\ttest" | python3 -m sockeye.lexical_constraints --avoid

    will produce the following JSON object (note the new keyword):

        { "text": "Das ist ein Test .", "avoid": ["This is", "test"] }

    Make sure you apply all preprocessing (tokenization, BPE, etc.) to both the source and the target-side constraints.
    You can then translate this object by passing it to Sockeye on STDIN as follows:

        python3 -m sockeye.translate -m /path/to/model --json-input --beam-size 20 --beam-prune 20

    Note the recommended Sockeye parameters. Beam pruning isn't needed for negative constraints.
    """
    import sys
    import json

    for line in sys.stdin:
        line = line.rstrip()

        # Constraints are in fields 2+
        source, *restrictions = line.split('\t')

        obj = {'text': source}
        constraints = []
        avoid_list = []
        for item in restrictions:
            if args.avoid:
                avoid_list.append(item)
            else:
                constraints.append(item)

        if constraints:
            obj['constraints'] = constraints
        if avoid_list:
            obj['avoid'] = avoid_list

        print(json.dumps(obj, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--avoid', action='store_true', help='Constraints are negative constraints')
    args = parser.parse_args()

    main(args)
