# ******************************************************************************
# Copyright 2017-2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ******************************************************************************

import logging
import os
from typing import List, Union

import numpy as np
import torch
from pytorch_transformers import (BertForSequenceClassification,
                                  XLMForSequenceClassification,
                                  XLNetForSequenceClassification)
from torch.utils.data import DataLoader, SequentialSampler, TensorDataset

from nlp_architect.data.sequence_classification import SequenceClsInputExample
from nlp_architect.models.transformers.base_model import (InputFeatures,
                                                          TransformerBase)
from nlp_architect.models.transformers.quantized_bert import QuantizedBertForSequenceClassification
from nlp_architect.utils.metrics import accuracy

logger = logging.getLogger(__name__)


class TransformerSequenceClassifier(TransformerBase):
    """
    Transformer sequence classifier

    Args:
        model_type (str): transformer base model type
        labels (List[str], optional): list of labels. Defaults to None.
        task_type (str, optional): task type (classification/regression). Defaults to
        classification.
        metric_fn ([type], optional): metric to use for evaluation. Defaults to
        simple_accuracy.
    """
    MODEL_CLASS = {
        'bert': BertForSequenceClassification,
        'quant_bert': QuantizedBertForSequenceClassification,
        'xlnet': XLNetForSequenceClassification,
        'xlm': XLMForSequenceClassification
    }

    def __init__(self, model_type: str, labels: List[str] = None,
                 task_type="classification", metric_fn=accuracy,
                 *args, **kwargs):
        assert model_type in self.MODEL_CLASS.keys(), "unsupported model type"
        self.labels = labels
        self.num_labels = len(labels)
        super(TransformerSequenceClassifier, self).__init__(model_type,
                                                            labels=labels,
                                                            num_labels=self.num_labels, *args,
                                                            **kwargs)
        self.model_class = self.MODEL_CLASS[model_type]
        self.model = self.model_class.from_pretrained(self.model_name_or_path, from_tf=bool(
            '.ckpt' in self.model_name_or_path), config=self.config)
        self.task_type = task_type
        self.metric_fn = metric_fn
        self.to(self.device, self.n_gpus)

    def train(self,
              train_data_set: DataLoader,
              dev_data_set: Union[DataLoader, List[DataLoader]] = None,
              test_data_set: Union[DataLoader, List[DataLoader]] = None,
              gradient_accumulation_steps: int = 1,
              per_gpu_train_batch_size: int = 8,
              max_steps: int = -1,
              num_train_epochs: int = 3,
              max_grad_norm: float = 1.0,
              logging_steps: int = 50,
              save_steps: int = 100):
        """
        Train a model

        Args:
            train_data_set (DataLoader): training data set
            dev_data_set (Union[DataLoader, List[DataLoader]], optional): development set.
            Defaults to None.
            test_data_set (Union[DataLoader, List[DataLoader]], optional): test set.
            Defaults to None.
            gradient_accumulation_steps (int, optional): num of gradient accumulation steps.
            Defaults to 1.
            per_gpu_train_batch_size (int, optional): per GPU train batch size. Defaults to 8.
            max_steps (int, optional): max steps. Defaults to -1.
            num_train_epochs (int, optional): number of train epochs. Defaults to 3.
            max_grad_norm (float, optional): max gradient normalization. Defaults to 1.0.
            logging_steps (int, optional): number of steps between logging. Defaults to 50.
            save_steps (int, optional): number of steps between model save. Defaults to 100.
        """
        self._train(train_data_set,
                    dev_data_set,
                    test_data_set,
                    gradient_accumulation_steps,
                    per_gpu_train_batch_size,
                    max_steps,
                    num_train_epochs,
                    max_grad_norm,
                    logging_steps=logging_steps,
                    save_steps=save_steps)

    def evaluate_predictions(self, logits, label_ids):
        """
        Run evaluation of given logits and truth labels

        Args:
            logits: model logits
            label_ids: truth label ids
        """
        preds = self._postprocess_logits(logits)
        label_ids = label_ids.numpy()
        result = self.metric_fn(preds, label_ids)
        try:
            output_eval_file = os.path.join(
                self.output_path, "eval_results.txt")
        except TypeError:
            output_eval_file = os.devnull
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

    def convert_to_tensors(self,
                           examples: List[SequenceClsInputExample],
                           max_seq_length: int = 128,
                           include_labels: bool = True) -> TensorDataset:
        """
        Convert examples to tensor dataset

        Args:
            examples (List[SequenceClsInputExample]): examples
            max_seq_length (int, optional): max sequence length. Defaults to 128.
            include_labels (bool, optional): include labels. Defaults to True.

        Returns:
            TensorDataset:
        """
        features = self._convert_examples_to_features(examples,
                                                      max_seq_length,
                                                      self.tokenizer,
                                                      self.task_type,
                                                      include_labels,
                                                      cls_token_at_end=bool(
                                                          self.model_type in ['xlnet']),
                                                      cls_token=self.tokenizer.cls_token,
                                                      sep_token=self.tokenizer.sep_token,
                                                      cls_token_segment_id=2 if
                                                      self.model_type in ['xlnet'] else 1,
                                                      # pad on the left for xlnet
                                                      pad_on_left=bool(
                                                          self.model_type in ['xlnet']),
                                                      pad_token_segment_id=4
                                                      if self.model_type in ['xlnet'] else 0)
        # Convert to Tensors and build dataset
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        if include_labels:
            if self.task_type == "classification":
                all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
            elif self.task_type == "regression":
                all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.float)
            return TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids)
        return TensorDataset(all_input_ids, all_input_mask, all_segment_ids)

    def inference(self, examples: List[SequenceClsInputExample], batch_size: int = 64, evaluate=False):
        """
        Run inference on given examples

        Args:
            examples (List[SequenceClsInputExample]): examples
            batch_size (int, optional): batch size. Defaults to 64.

        Returns:
            logits
        """
        data_set = self.convert_to_tensors(examples, include_labels=evaluate)
        inf_sampler = SequentialSampler(data_set)
        inf_dataloader = DataLoader(data_set, sampler=inf_sampler, batch_size=batch_size)
        logits = self._evaluate(inf_dataloader)
        if not evaluate:
            preds = self._postprocess_logits(logits)
        else:
            logits, label_ids = logits
            preds = self._postprocess_logits(logits)
            self.evaluate_predictions(logits, label_ids)
        return preds

    def _postprocess_logits(self, logits):
        preds = logits.numpy()
        if self.task_type == "classification":
            preds = np.argmax(preds, axis=1)
        elif self.task_type == "regression":
            preds = np.squeeze(preds)
        return preds

    def _convert_examples_to_features(self, examples, max_seq_length,
                                      tokenizer, task_type, include_labels=True,
                                      cls_token_at_end=False, pad_on_left=False,
                                      cls_token='[CLS]', sep_token='[SEP]', pad_token=0,
                                      sequence_a_segment_id=0, sequence_b_segment_id=1,
                                      cls_token_segment_id=1, pad_token_segment_id=0,
                                      mask_padding_with_zero=True):
        """ Loads a data file into a list of `InputBatch`s
            `cls_token_at_end` define the location of the CLS token:
                - False (Default, BERT/XLM pattern): [CLS] + A + [SEP] + B + [SEP]
                - True (XLNet/GPT pattern): A + [SEP] + B + [SEP] + [CLS]
            `cls_token_segment_id` define the segment id associated to the CLS token
            (0 for BERT, 2 for XLNet)
        """

        if include_labels:
            label_map = {label: i for i, label in enumerate(self.labels)}

        features = []
        for (ex_index, example) in enumerate(examples):
            if ex_index % 10000 == 0:
                logger.info("Writing example %d of %d" % (ex_index, len(examples)))

            tokens_a = tokenizer.tokenize(example.text)

            tokens_b = None
            if example.text_b:
                tokens_b = tokenizer.tokenize(example.text_b)
                # Modifies `tokens_a` and `tokens_b` in place so that the total
                # length is less than the specified length.
                # Account for [CLS], [SEP], [SEP] with "- 3"
                _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
            else:
                # Account for [CLS] and [SEP] with "- 2"
                if len(tokens_a) > max_seq_length - 2:
                    tokens_a = tokens_a[:(max_seq_length - 2)]

            # The convention in BERT is:
            # (a) For sequence pairs:
            #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
            #  type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1
            # (b) For single sequences:
            #  tokens:   [CLS] the dog is hairy . [SEP]
            #  type_ids:   0   0   0   0  0     0   0
            #
            # Where "type_ids" are used to indicate whether this is the first
            # sequence or the second sequence. The embedding vectors for `type=0` and
            # `type=1` were learned during pre-training and are added to the wordpiece
            # embedding vector (and position vector). This is not *strictly* necessary
            # since the [SEP] token unambiguously separates the sequences, but it makes
            # it easier for the model to learn the concept of sequences.
            #
            # For classification tasks, the first vector (corresponding to [CLS]) is
            # used as as the "sentence vector". Note that this only makes sense because
            # the entire model is fine-tuned.
            tokens = tokens_a + [sep_token]
            segment_ids = [sequence_a_segment_id] * len(tokens)

            if tokens_b:
                tokens += tokens_b + [sep_token]
                segment_ids += [sequence_b_segment_id] * (len(tokens_b) + 1)

            if cls_token_at_end:
                tokens = tokens + [cls_token]
                segment_ids = segment_ids + [cls_token_segment_id]
            else:
                tokens = [cls_token] + tokens
                segment_ids = [cls_token_segment_id] + segment_ids

            input_ids = tokenizer.convert_tokens_to_ids(tokens)

            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

            # Zero-pad up to the sequence length.
            padding_length = max_seq_length - len(input_ids)
            if pad_on_left:
                input_ids = ([pad_token] * padding_length) + input_ids
                input_mask = ([0 if mask_padding_with_zero else 1] * padding_length) + input_mask
                segment_ids = ([pad_token_segment_id] * padding_length) + segment_ids
            else:
                input_ids = input_ids + ([pad_token] * padding_length)
                input_mask = input_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
                segment_ids = segment_ids + ([pad_token_segment_id] * padding_length)

            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            if include_labels:
                if task_type == "classification":
                    label_id = label_map[example.label]
                elif task_type == "regression":
                    label_id = float(example.label)
                else:
                    raise KeyError(task_type)
            else:
                label_id = None

            features.append(InputFeatures(input_ids=input_ids,
                                          input_mask=input_mask,
                                          segment_ids=segment_ids,
                                          label_id=label_id))
        return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""
    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()
