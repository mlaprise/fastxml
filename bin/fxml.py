#!/usr/bin/env python
from __future__ import print_function
import math
import sys
import json
import pprint
import os
from collections import defaultdict, Counter
import multiprocessing
from itertools import islice, chain, count
import cPickle

import argparse

import numpy as np
from sklearn.feature_extraction import FeatureHasher
import scipy.sparse as sp

from fastxml import FastXML
from fastxml.fastxml import metric_cluster
from fastxml.weights import uniform, nnllog, propensity

def build_arg_parser():
    parser = argparse.ArgumentParser(description='FastXML trainer and tester',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("model", 
        help="Model to use for dataset file")

    parser.add_argument("input_file", 
        help="Input file to use")

    parser.add_argument("--standard-dataset", dest="standardDataset", action="store_true",
        help="Input is standard dataset sparse format")
    
    parser.add_argument("--verbose", action="store_true",
        help="Verbose"
    )

    subparsers = parser.add_subparsers(dest="command")

    trainer = subparsers.add_parser('train', help="Set up for trainer")
    build_train_parser(trainer)

    inference = subparsers.add_parser('inference', help="Runs a model against a dataset")
    build_repl_parser(inference)
    build_inference_parser(inference)

    cluster = subparsers.add_parser('cluster', help="Clusters labels into NDCG classes")
    build_cluster_parser(cluster)

    repl = subparsers.add_parser('repl', help="Interactive mode for a model")
    build_repl_parser(repl)

    return parser

def build_cluster_parser(parser):
    parser.add_argument("--trees", dest="trees", type=int, default=1,
        help="Number of random trees to cluster on"
    )
    parser.add_argument("--label-weight", dest="label_weight", 
        choices=('uniform', 'nnllog', 'propensity'), default='propensity',
        help="Metric for computing label weighting."
    )
    parser.add_argument("--max_leaf_size", dest="max_leaf_size", type=int,
        default=10,
        help="Maximumum number of examples allowed per leaf"
    )

def build_repl_parser(parser):
    parser.add_argument("--max-predict", dest="max_predict", type=int,
        default=10,
        help="Maximum number of classes to predict"
    )
    parser.add_argument("--gamma", type=float,
        help="Overrides default gamma value for leaf classifiers"
    )
    parser.add_argument("--blend_factor", type=float,
        help="Overrides default blend factor"
    )
    parser.add_argument("--tree", type=lambda x: map(int, x.split(',')),
        help="Tests a particular tree set in the ensemble.  Default is all"
    )

def build_inference_parser(parser):
    parser.add_argument("--dict", dest="dict", action="store_true",
        help="Store predict as dict"
    )
    parser.add_argument("--score", action="store_true",
        help="Scores results according to ndcg and precision"
    )

def build_train_parser(parser):
    parser.add_argument("--engine", dest="engine", default="auto",
        choices=('auto', 'sgd', 'liblinear'),
        help="Which engine to use."
    )
    parser.add_argument("--auto-weight", dest="auto_weight", default=32, type=int,
        help="When engine is 'auto', number of classes * max_leaf_size remaining to revert to SGD"
    )
    parser.add_argument("--no-remap-labels", dest="noRemap", action="store_true",
        help="Whether to remap labels to an internal format.  Needed for string labels"
    )
    parser.add_argument("--trees", dest="trees", type=int,
        default=50,
        help="Number of trees to use"
    )
    parser.add_argument("--max_leaf_size", dest="max_leaf_size", type=int,
        default=10,
        help="Maximumum number of examples allowed per leaf"
    )
    parser.add_argument("--max_labels_per_leaf", dest="max_labels_per_leaf", type=int,
        default=50,
        help="Maximum number of classes to retaion for probability distribution per leaf"
    )
    parser.add_argument("--re_split", dest="re_split", type=int,
        default=1,
        help="After fitting a classifier, re-splits the data according to fitted "\
             "classifier.  If greater than 1, it will re-fit and re-train a classifier "\
             "the data if after splitting, it all ends in a leaf.  Will retry N times."
    )
    parser.add_argument("--alpha", dest="alpha", type=float,
        default=1e-3,
        help="L1 coefficient.  Too high and it won't learn a split, too low and "\
             "it won't be sparse (larger file size, slower inference)."
    )
    parser.add_argument("--C", dest="C", type=float,
        default=1,
        help="C value for when using auto, penalizing accuracy over fit"
    )
    parser.add_argument("--iters", dest="iters", type=int,
        default=2,
        help="Number of iterations to run over the dataset when fitting classifier"
    )
    parser.add_argument("--no_bias", dest="bias", action="store_false",
        help="Fits a bias for the classifier.  Not needed if data has E[X] = 0"
    )
    parser.add_argument("--subsample", dest="subsample", type=float,
        default=1.0,
        help="Subsample data per tree.  if less than 1, interpretted as a "\
             "percentage.  If greater than one, taken as number of data " \
             "points per tree."
    )
    parser.add_argument("--loss", dest="loss", choices=('log', 'hinge'),
        default='log',
        help="Loss to minimize."
    )
    parser.add_argument("--threads", dest="threads", type=int,
        default=multiprocessing.cpu_count(),
        help="Number of threads to use.  Will use min(threads, trees)"
    )
    parser.add_argument("--label-weight", dest="label_weight", 
        choices=('uniform', 'nnllog', 'propensity'), default='propensity',
        help="Metric for computing label weighting."
    )
    parser.add_argument("--optimization", dest="optimization", 
        choices=('fastxml', 'dsimec'), default='fastxml',
        help="optimization strategy to use for linear classifier"
    )
    parser.add_argument("--eps", dest="eps", type=float,
        help="Sparsity epsilon.  Weights lower than eps will suppress to zero"
    )
    parser.add_argument("--leaf-classifiers", dest="leaf_class", 
        action="store_true",
        help="Whether to use and compute leaf classifiers"
    )
    parser.add_argument("--gamma", type=int, default=30,
        help="Gamma coefficient for hyper-sphere weighting"
    )
    parser.add_argument("--blend-factor", dest="blend_factor",
        type=float, default=0.5,
        help="blend * tree-probs + (1 - blend) * tail-classifiers"
    )
    parser.add_argument("--min-label-count", dest="mlc",
        type=int, default=5,
        help="Filter out labels with count < min-label-count"
    )
    parser.add_argument("--leaf-probs", dest="leafProbs",
        action="store_true",
        help="Computes probability: TP(X) * LP(X)"
    )
    return parser

def sliding(it, window):
    x = list(islice(it, window))
    try:
        if len(x) == window:
            while True:
                yield x
                x2 = x[1:]
                x2.append(next(it))
                x = x2

    except StopIteration:
        pass

class Quantizer(object):
    def stream(self, fn):
        raise NotImplementedError()

class JsonQuantizer(Quantizer):
    def __init__(self, verbose, min_label_count=1, inference=False):
        self.fh = FeatureHasher(dtype='float32')
        self.verbose = verbose
        self.inference = inference
        self.min_label_count = min_label_count

    def quantize(self, text):
        text = text.lower().replace(',', '')
        unigrams = text.split()
        bigrams = (' '.join(xs) for xs in sliding(iter(unigrams), 2))
        trigrams = (' '.join(xs) for xs in sliding(iter(unigrams), 3))
        
        d = {f: 1.0 for f in chain(unigrams, bigrams, trigrams)}
        return self.fh.transform([d])

    def yieldJson(self, fname):
        with file(fname) as f:
            for i, line in enumerate(f):
                if self.verbose and i % 10000 == 0:
                    print("%s docs encoded" % i)

                yield json.loads(line)

    def count_labels(self, fname):
        c = Counter()
        for data in self.yieldJson(fname):
            c.update(data['tags'])

        return (lambda t: c[t] >= self.min_label_count)

    def stream(self, fname):
        if self.min_label_count > 1:
            f = self.count_labels(fname)
        else:
            f = lambda x: True

        for data in self.yieldJson(fname):
            if not data.get('title'):
                continue

            if not self.inference and not data.get('tags'):
                continue
                
            X = self.quantize(data['title'])

            y = [yi for yi in set(data.get('tags', [])) if f(yi)]
            if y or self.inference:
                yield data, X, y

class StandardDatasetQuantizer(Quantizer):
    def __init__(self, verbose):
        self.verbose = verbose

    def quantize(self, line):
        classes, sparse = line.strip().split(None, 1) 
        y = map(int, classes.split(','))
        c, d = [], [] 
        for v in sparse.split():
            loc, v = v.split(":")
            c.append(int(loc))
            d.append(float(v))

        return (c, d), y

    def stream(self, fn):
        with file(fn) as f:
            n_samples, n_feats, n_classes = map(int, f.readline().split())
            for i, line in enumerate(f):
                if ',' not in line:
                    continue

                if self.verbose and i % 10000 == 0:
                    print("%s docs encoded" % i)

                (c, d), y = self.quantize(line)
                yield {"labels": y}, sp.csr_matrix((d, ([0] * len(d), c)), 
                        shape=(1, n_feats), dtype='float32'), y

class Dataset(object):
    def __init__(self, dataset):
        self.dataset = dataset

    @property
    def model(self):
        return os.path.join(self.dataset, 'model')

    @property
    def classes(self):
        return os.path.join(self.dataset, 'counts')

def quantize(args, quantizer, classes):
    cnt = count()
    for _, X, ys in quantizer.stream(args.input_file):
        nys = []
        for y in ys:
            if y not in classes:
                classes[y] = y if getattr(args, 'noRemap', False) else next(cnt)

            nys.append(classes[y])
        
        yield X, nys

def train(args, quantizer):
    cnt = count()
    classes, X_train, y_train = {}, [], []
    for X, y in quantize(args, quantizer, classes):
        X_train.append(X)
        y_train.append(y)

    # Save the mapping
    dataset = Dataset(args.model)
    if not os.path.isdir(args.model):
        os.makedirs(args.model)

    with file(dataset.classes, 'w') as out:
        json.dump(classes.items(), out)

    # Train
    clf = FastXML(
        n_trees=args.trees,
        max_leaf_size=args.max_leaf_size,
        max_labels_per_leaf=args.max_labels_per_leaf,
        re_split=args.re_split,
        alpha=args.alpha,
        n_epochs=args.iters,
        bias=args.bias,
        subsample=args.subsample,
        loss=args.loss,
        leaf_classifiers=args.leaf_class,
        blend=args.blend_factor,
        gamma=args.gamma,
        n_jobs=args.threads,
        optimization=args.optimization,
        eps=args.eps,
        C=args.C,
        engine=args.engine,
        auto_weight=args.auto_weight,
        leaf_probs=args.leafProbs,
        verbose=args.verbose
    )

    weights = compute_weights(y_train, args.label_weight)
    clf.fit(X_train, y_train, weights=weights)

    with file(dataset.model, 'w') as out:
        cPickle.dump(clf, out, cPickle.HIGHEST_PROTOCOL)

    sys.exit(0)

def compute_weights(y_train, label_weight):
    if label_weight == 'nnllog':
        return nnllog(y_train)
    elif label_weight == 'uniform':
        return uniform(y_train)
    elif label_weight == 'propensity':
        return propensity(y_train)
    else:
        raise NotImplementedError(label_weight)

def dcg(scores, k=None):
    if k is not None:
        scores = scores[:k]

    return sum(rl / math.log(i + 2) for i, rl in enumerate(scores))

def ndcg(scores, k=None, eps=1e-6):
    idcgs = dcg(sorted(scores, reverse=True), k)
    if idcgs < eps:
        return 0.0

    dcgs = dcg(scores, k)

    return dcgs / idcgs

def print_ndcg(ndcgs):
    ndcgT = zip(*ndcgs)
    for i in xrange(3):
        print('NDCG@{}: {}'.format(2 * i + 1, np.mean(ndcgT[i])), file=sys.stderr)

    print(file=sys.stderr)

def loadClasses(dataset):
    # Load reverse map
    with file(dataset.classes) as f:
        data = json.load(f)
        return {v: k for k, v in data}


def inference(args, quantizer):
    dataset = Dataset(args.model)

    with file(dataset.model) as f:
        clf = cPickle.load(f)

    if args.blend_factor is not None:
        clf.blend = args.blend_factor

    if args.gamma is not None:
        clf.gamma = args.gamma

    classes = loadClasses(dataset)

    ndcgs = []
    for data, X, y in quantizer.stream(args.input_file):
        y_hat = clf.predict(X, 'dict', args.tree)[0]
        yi = islice(y_hat.iteritems(), args.max_predict)
        nvals = [[unicode(classes[k]), v] for k, v in yi]
        data['predict'] = dict(nvals) if args.dict else nvals

        if args.score:
            ys = set(y)
            scores = []
            for yii in y_hat.iterkeys():
                if classes[yii] in ys:
                    ys.remove(classes[yii])
                    scores.append(1)
                else:
                    scores.append(0)

            scores.extend([1] * len(ys))

            ndcgs.append([ndcg(scores, i) for i in (1, 3, 5)])
            data['ndcg'] = ndcgs[-1]

            if len(ndcgs) % 100 == 0:
                print("Seen:", len(ndcgs), file=sys.stderr)
                print_ndcg(ndcgs)

        print(json.dumps(data))

    if args.score:
        print_ndcg(ndcgs)

def cluster(args, quantizer):
    classes, y_train = {}, []
    for X, y in quantize(args, quantizer, classes):
        y_train.append(y)

    classes = {v: k for k, v in classes.iteritems()}
    weights = compute_weights(y_train, args.label_weight)
    trees = []
    for i in xrange(args.trees):
        tree = metric_cluster(y_train, weights=weights, 
                max_leaf_size=args.max_leaf_size,
                seed=2016 + i, verbose=args.verbose)

        d = tree.build_discrete()[1]
        k, p = tree.build_probs(y_train)
        for i in xrange(k):
            x = {classes[l]: round(ps, 3) for l, ps in p[i][1].iteritems()}
            print("Prob", i, json.dumps(x))

        td = {idx: tn for tn, idxs in d for idx in idxs}
        trees.append(td)

    for i in xrange(weights.shape[0]):
        cluster = [t[i] for t in trees]
        print(i, ' '.join(str(c) for c in cluster))
    
def repl(args, quantizer):
    dataset = Dataset(args.model)

    with file(dataset.model) as f:
        clf = cPickle.load(f)

    if args.blend_factor is not None:
        clf.blend = args.blend_factor

    if args.gamma is not None:
        clf.gamma = args.gamma

    classes = loadClasses(dataset)

    try:
        while True:
            title = raw_input("> ")
            X = quantizer.quantize(title)
            y_hat = clf.predict(X, 'dict')[0]
            yi = islice(y_hat.iteritems(), args.max_predict)
            nvals = [[unicode(classes[k]), v] for k, v in yi]
            pprint.pprint(nvals)

    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    args = build_arg_parser().parse_args()
    # Quantize
    if args.standardDataset:
        quantizer = StandardDatasetQuantizer(args.verbose)
    else:
        mlc = args.mlc if args.command == 'train' else 1
        quantizer = JsonQuantizer(args.verbose, mlc, args.command == 'inference')

    if args.command == 'train':
        train(args, quantizer)
    elif args.command == 'inference':
        inference(args, quantizer)
    elif args.command == 'repl':
        repl(args, quantizer)
    elif args.command == 'cluster':
        cluster(args, quantizer)
