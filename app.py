# coding=utf8
"""================================
@Author: Mr.Chang
@Date  : 2021/9/3 4:58 下午
==================================="""
import logging
from itertools import chain

import random

import torch
from flask import Flask
from flask import request, jsonify
from transformers import BertTokenizer, OpenAIGPTLMHeadModel, GPT2LMHeadModel
import torch.nn.functional as F
from argparse import ArgumentParser

SPECIAL_TOKENS = ["[CLS]", "[SEP]", "[PAD]", "[speaker1]", "[speaker2]"]

app = Flask(__name__)


class Dialog(object):

    def __init__(self, model_dir):
        self.history = []

        self._init_dialog(model_dir)

    def _init_dialog(self, model_dir):
        self.gpt2 = False
        self.model_checkpoint = model_dir
        self.max_history = 5
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.no_sample = False
        self.max_length = 30
        self.min_length = 1
        self.seed = 42
        self.temperature = 0.7
        self.top_k = 3
        self.top_p = 0.9

        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger(__file__)

        random.seed(self.seed)
        torch.random.manual_seed(self.seed)
        torch.cuda.manual_seed(self.seed)

        logger.info("Get pretrained model and tokenizer")
        tokenizer_class = BertTokenizer
        model_class = OpenAIGPTLMHeadModel if not self.gpt2 else GPT2LMHeadModel
        self.tokenizer = tokenizer_class.from_pretrained(self.model_checkpoint, do_lower_case=True)
        self.model = model_class.from_pretrained(self.model_checkpoint)

        self.model.to(self.device)
        self.model.eval()

    def tokenize(self, obj):
        if isinstance(obj, str):
            return self.tokenizer.convert_tokens_to_ids(self.tokenizer.tokenize(obj))
        if isinstance(obj, dict):
            return dict((n, self.tokenize(o)) for n, o in obj.items())
        return list(self.tokenize(o) for o in obj)

    def get_result(self, text):

        raw_text = text
        raw_text = ' '.join(list(raw_text.replace(" ", "")))
        self.history.append(self.tokenize(raw_text))
        print(self.history)
        with torch.no_grad():
            out_ids = self.sample_sequence()

        self.history.append(out_ids)
        self.history = self.history[-(2 * self.max_history + 1):]
        out_text = self.tokenizer.decode(out_ids, skip_special_tokens=True)
        out_text = out_text.replace(" ", "")

        return out_text

    def sample_sequence(self, current_output=None):
        special_tokens_ids = self.tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)
        if current_output is None:
            current_output = []

        for i in range(self.max_length):
            instance, sequence = self.build_input_from_segments(current_output,  with_eos=False)
            input_ids = torch.tensor(instance["input_ids"], dtype=torch.long, device=self.device).unsqueeze(0)
            token_type_ids = torch.tensor(instance["token_type_ids"], dtype=torch.long, device=self.device).unsqueeze(0)

            logits, *_ = self.model(input_ids, token_type_ids=token_type_ids)
            logits = logits[0, -1, :] / self.temperature
            logits = self.top_filtering(logits, top_k=self.top_k, top_p=self.top_p)
            probs = F.softmax(logits, dim=-1)

            prev = torch.topk(probs, 1)[1] if self.no_sample else torch.multinomial(probs, 1)
            if i < self.min_length and prev.item() in special_tokens_ids:
                while prev.item() in special_tokens_ids:
                    prev = torch.multinomial(probs, num_samples=1)

            if prev.item() in special_tokens_ids:
                break
            current_output.append(prev.item())

        return current_output

    def top_filtering(self, logits, top_k=0, top_p=0.0, threshold=-float('Inf'), filter_value=-float('Inf')):
        """ Filter a distribution of logits using top-k, top-p (nucleus) and/or threshold filtering
            Args:
                logits: logits distribution shape (vocabulary size)
                top_k: <=0: no filtering, >0: keep only top k tokens with highest probability.
                top_p: <=0.0: no filtering, >0.0: keep only a subset S of candidates, where S is the smallest subset
                    whose total probability mass is greater than or equal to the threshold top_p.
                    In practice, we select the highest probability tokens whose cumulative probability mass exceeds
                    the threshold top_p.
                threshold: a minimal threshold to keep logits
        """
        assert logits.dim() == 1  # Only work for batch size 1 for now - could update but it would obfuscate a bit the code
        top_k = min(top_k, logits.size(-1))
        if top_k > 0:
            # Remove all tokens with a probability less than the last token in the top-k tokens
            indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
            logits[indices_to_remove] = filter_value

        if top_p > 0.0:
            # Compute cumulative probabilities of sorted tokens
            sorted_logits, sorted_indices = torch.sort(logits, descending=True)
            cumulative_probabilities = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

            # Remove tokens with cumulative probability above the threshold
            sorted_indices_to_remove = cumulative_probabilities > top_p
            # Shift the indices to the right to keep also the first token above the threshold
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
            sorted_indices_to_remove[..., 0] = 0

            # Back to unsorted indices and set them to -infinity
            indices_to_remove = sorted_indices[sorted_indices_to_remove]
            logits[indices_to_remove] = filter_value

        indices_to_remove = logits < threshold
        logits[indices_to_remove] = filter_value

        return logits

    def build_input_from_segments(self, reply, with_eos=True):
        """ Build a sequence of input from 3 segments: persona, history and last reply """
        # global SPECIAL_TOKENS
        bos, eos, pad, speaker1, speaker2 = self.tokenizer.convert_tokens_to_ids(SPECIAL_TOKENS)
        sequence = [[bos]] + self.history + [reply + ([eos] if with_eos else [])]
        print(sequence)
        sequence = [sequence[0]] + [[speaker2 if i % 2 else speaker1] + s for i, s in enumerate(sequence[1:])]
        input_ids = list(chain(*sequence))
        token_types = [bos] + [speaker2 if i % 2 else speaker1 for i, s in enumerate(sequence[1:])
                                              for _ in s]
        ids_length = len(input_ids)
        # 保证总长度小于512
        if ids_length > 512:
            input_ids_ = [input_ids[0]]
            token_types_ = [token_types[0]]
            input_ids_.extend(input_ids[ids_length - 511:])
            token_types_.extend(token_types[ids_length - 511:])

            instance = {}
            instance["input_ids"] = input_ids_
            instance["token_type_ids"] = token_types_
            return instance, sequence

        instance = {}
        instance["input_ids"] = input_ids
        instance["token_type_ids"] = token_types
        return instance, sequence



@app.route('/talk', methods=['POST'])
def talk():

    try:
        text = request.get_json()['text']
        result = dialog.get_result(text)
        return jsonify(text=result), 200
    except Exception as e:
        return jsonify(error='something was wrong: {}'.format(e)), 400


if __name__ == '__main__':
    args = ArgumentParser()
    args.add_argument('--model_dir', type=str, help='model directory')
    parser = args.parse_args()
    dialog = Dialog(parser.model_dir)
    app.run(host="0.0.0.0", port=8088)
