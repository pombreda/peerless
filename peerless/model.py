# -*- coding: utf-8 -*-

from __future__ import division, print_function

__all__ = ["Model"]

import os
import h5py
import pickle
import transit
import logging
import numpy as np
from scipy.stats import beta
from scipy.spatial import cKDTree
from collections import defaultdict

from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import auc, precision_recall_curve

try:
    import pywt
except ImportError:
    pywt = None


class Model(object):

    def __init__(self, lcs, wavelet=None, half_width=100, **kwargs):
        self.lcs = [lc for lc in lcs if len(lc) > 2*int(half_width)]
        self.half_width = int(half_width)
        self.models = [None] * 3
        self.X = None
        self.kwargs = kwargs
        self.wavelet = wavelet
        if wavelet is not None and pywt is None:
            raise ImportError("pywt")

        # Pre-compute the footprint weights for each light curve.
        w = np.array([lc.footprint for lc in self.lcs])
        w /= np.sum(w)
        self.weights = w

        # Split the sections into the three sets.
        quarters = np.array([lc.meta["quarter"] for lc in self.lcs])
        inds = np.arange(len(quarters))
        self.splits = [set() for _ in range(3)]

        # Loop over each quarter and distribute the sections "uniformly".
        for q in set(quarters):
            m = quarters == q
            i = inds[m]
            if m.sum() < 3:
                # If there are fewer than 3 sections, randomly add them.
                j = np.random.choice(3, size=m.sum(), replace=False)
                for j0, i0 in zip(j, i):
                    self.splits[j0].add(i0)
            elif m.sum() == 3:
                # If there are exactly 3 sections, add one to each split.
                np.random.shuffle(i)
                for j0, i0 in enumerate(i):
                    self.splits[j0].add(i0)
            else:
                # If there are more than 3 sections, distribute them according
                # to their footprints... this should end up having
                # approximately the same footprint of data in each split.
                np.random.shuffle(i)
                w = self.weights[i]
                w /= w.sum()
                cs = np.cumsum(w)
                a, b = np.argmin(np.abs(cs-1./3)), np.argmin(np.abs(cs-2./3))
                self.splits[0] |= set(i[:a+1])
                self.splits[1] |= set(i[a+1:b+1])
                self.splits[2] |= set(i[b+1:])

        logging.info("Found splits {0}".format(map(len, self.splits)))

    def format_dataset(self, npos=20000, nneg=None,
                       min_period=1.0e3, max_period=5.0e3,
                       min_rad=0.05, max_rad=0.2, dt=0.15,
                       smass=1.0, srad=1.0):
        lcs = self.lcs
        if nneg is None:
            nneg = npos

        # The time grid for within a single chunk.
        inds = np.arange(-self.half_width, self.half_width+1)
        meta_keys = ["channel", "skygroup", "module", "output", "quarter",
                     "season"]

        logging.info("Generating training and validation sets")
        pos_sims = np.empty((3, npos, len(inds)))
        pos_pars = [[] for _ in range(3)]
        neg_sims = np.empty((3, nneg, len(inds)))
        neg_pars = [[] for _ in range(3)]
        for i in range(3):
            lc_inds = np.array(list(self.splits[i]))
            pval = self.weights[lc_inds]
            pval /= pval.sum()

            for j in range(pos_sims.shape[1]):
                # Generate the simulation parameters.
                nlc = np.random.choice(lc_inds, p=pval)
                ntt = np.random.randint(self.half_width,
                                        len(lcs[nlc])-self.half_width)
                rp = np.exp(np.random.uniform(np.log(min_rad),
                                              np.log(max_rad)))
                pos_pars[i].append([
                    nlc, ntt, lcs[nlc].time[ntt],
                    np.random.rand(), np.random.rand(),
                    np.exp(np.random.uniform(np.log(min_period),
                                             np.log(max_period))),
                    np.random.uniform(-dt, dt),
                    rp,
                    np.random.uniform(0, 1.0 - rp / srad),
                    0.0, 0.0,
                    # beta.rvs(0.867, 3.03),
                    # np.random.uniform(-np.pi, np.pi),
                ] + [lcs[nlc].meta[k] for k in meta_keys])

                # Build the simulator and inject the transit signal.
                s = simulation_system(smass, srad,
                                      *(pos_pars[i][j][3:-len(meta_keys)]))
                t = lcs[nlc].time[ntt+inds]
                t -= np.mean(t)
                order = 2*np.random.randint(2)-1
                pos_sims[i, j] = lcs[nlc].flux[ntt+inds][::order]
                pos_sims[i, j] *= s.light_curve(t, texp=lcs[nlc].texp)

            for j in range(neg_sims.shape[1]):
                nlc = np.random.choice(lc_inds, p=pval)
                ntt = np.random.randint(self.half_width,
                                        len(lcs[nlc])-self.half_width)
                neg_pars[i].append([nlc, ntt, lcs[nlc].time[ntt]] + 8*[np.nan]
                                   + [lcs[nlc].meta[k] for k in meta_keys])
                order = 2*np.random.randint(2)-1
                neg_sims[i, j] = lcs[nlc].flux[ntt+inds][::order]

            pos_sims[i] = self.normalize_inputs(pos_sims[i])
            neg_sims[i] = self.normalize_inputs(neg_sims[i])

        # Format the arrays for sklearn.
        X = np.concatenate((pos_sims, neg_sims), axis=1)
        y = np.ones(X.shape[:2])
        y[:, npos:] = 0

        # Give the metadata a dtype.
        dtype = [("sect_id", int), ("ntt", int), ("transit_time", float),
                 ("q1", float), ("q2", float), ("period", float),
                 ("t0", float), ("rp", float), ("b", float), ("e", float),
                 ("pomega", float)]
        dtype += [(k, int) for k in meta_keys]
        meta = [np.array(map(tuple, pos_pars[i] + neg_pars[i]), dtype=dtype)
                for i in range(3)]

        # Shuffle the order.
        inds = np.arange(X.shape[1])
        np.random.shuffle(inds)
        self.X = X[:, inds]
        self.y = y[:, inds]
        self.meta = [m[inds] for m in meta]

    def fit_all(self, **kwargs):
        return [self.fit_split(i, **kwargs) for i in range(3)]

    def fit_split(self, split, refit=False, cls=None, prec_req=1.0,
                  ntrain=None, **kwargs):
        # Return the cached model if it's already been computed.
        if not 0 <= split < len(self.models):
            raise ValueError("invalid split ID")
        if self.models[split] is not None and not refit:
            return self.models[split]

        # Generate the training data if required.
        if self.X is None:
            self.format_dataset(**(self.kwargs))

        # Set up the model.
        self.models[split] = d = dict(split_id=split)

        # Build the classifier.
        kwargs["n_estimators"] = kwargs.get("n_estimators", 1000)
        kwargs["min_samples_leaf"] = kwargs.get("min_samples_leaf", 2)
        if cls is None:
            cls = RandomForestClassifier
        d["classifier"] = clf = cls(**kwargs)

        # Only use the maximum number of training samples.
        X, y = self.X[split], self.y[split]
        if ntrain is not None:
            X, y = X[:ntrain], y[:ntrain]
            if len(X) != ntrain:
                logging.warn("Not enough training examples ({0})"
                             .format(len(X)))

        # Train the model.
        logging.info("Training the {0}-split model on {1} training examples"
                     .format(split, len(X)))
        clf.fit(X, y)

        # Compute the PR curve on the validation sets.
        vs = range(3)
        del vs[split]
        d["validation"] = [dict(split_id=i) for i in vs]
        d["test"] = [dict(split_id=i) for i in vs]
        for i, s in enumerate(vs):
            # Predict on the validation set.
            X_valid, y_valid = self.X[s], self.y[s]
            logging.info("Validating {0}-split model on {1} examples"
                         .format(split, len(X_valid)))
            y_valid_pred = clf.predict_proba(X_valid)[:, 1]

            # Save the predictions for the validation set.
            d0 = d["validation"][i]
            d0["validation_set"] = self.meta[s]
            d0["validation_pred"] = y_valid_pred

            # Compute the PR curve.
            prc = precision_recall_curve(y_valid, y_valid_pred)
            prc = np.array(zip(prc[0], prc[1], np.append(prc[2], 1.0)),
                           dtype=[("precision", float), ("recall", float),
                                  ("threshold", float)])
            d0["precision_recall_curve"] = prc

            # Compute the threshold and recall at fixed precision.
            d0["prec_req"] = prec_req
            d0["area_under_the_prc"] = auc(prc["recall"], prc["precision"])
            thr = prc["threshold"][prc["precision"] < prec_req]
            d0["threshold"] = thr[-1] if len(thr) else prc["threshold"][0]
            logging.info("AUC for {0}-split model: {1}"
                         .format(split, d0["area_under_the_prc"]))

            # Compute the prediction on the light curves that weren't used.
            logging.info("Computing prediction for test set")
            times, preds, sect_ids = [], [], []
            for j in self.splits[s]:
                t, f = self.unwrap_lc(self.lcs[j])
                X_test = self.normalize_inputs(f)
                times.append(t)
                preds.append(clf.predict_proba(X_test)[:, 1])
                sect_ids.append(j + np.zeros(len(t), dtype=int))
            d["test"][i]["prediction"] = np.array(zip(
                np.concatenate(sect_ids), np.concatenate(times),
                np.concatenate(preds),
            ), dtype=[("sect_id", int), ("time", float),
                      ("predict_prob", float)])

        return self.models[split]

    def find_candidates(self, window=4.0):
        if any(m is None for m in self.models):
            raise RuntimeError("you need to compute all the models first")

        # Build the KDTrees to find the nearest examples.
        inds = set(range(len(self.models)))
        X = [np.concatenate(self.X[list(inds - set([i]))], axis=0)
             for i in range(len(inds))]
        meta = [np.concatenate([
            self.meta[j] for j in list(inds - set([i]))
        ], axis=0) for i in range(len(inds))]
        trees = [cKDTree(x) for x in X]

        # Loop over the models and compete the models against each other.
        cand_time = defaultdict(list)
        cand_sect = dict()
        cand_pred = dict()
        for mod_ind, res in enumerate(self.models):
            for j, (test, valid) in enumerate(zip(res["test"],
                                                  res["validation"][::-1])):
                pred = test["prediction"]
                thresh = valid["threshold"]
                for i in np.arange(len(pred))[pred["predict_prob"] > thresh]:
                    t0 = pred["time"][i]
                    k = "{0:.6f}".format(t0)
                    cand_time[k].append(pred["predict_prob"][i] / thresh)
                    cand_sect[k] = pred["sect_id"][i]
                    if k in cand_pred:
                        continue

                    # Pull out the light curve chunk.
                    lc = self.lcs[pred["sect_id"][i]]
                    ind = np.argmin(np.abs(lc.time - t0))
                    xx = np.array([lc.flux[ind-self.half_width:
                                           ind+self.half_width+1]])
                    xx = self.normalize_inputs(xx)
                    _, ind = trees[test["split_id"]].query(xx)
                    cand_pred[k] = meta[test["split_id"]][ind]

        # Only include the models where more than one prediction agrees.
        meta_keys = ["channel", "skygroup", "module", "output", "quarter",
                     "season"]
        dtype = [("time", float), ("num_points", int), ("sect_id", int),
                 ("mean_factor", float)]
        dtype += [("factor_{0}".format(i+1), float)
                  for i in range(len(self.models) - 1)]
        dtype += [(k, int) for k in meta_keys]
        dt = meta[0][0].dtype
        inj_keys = dt.names
        dtype += [("nn_" + k, dt[k]) for k in inj_keys]
        candidates = np.array([tuple(
            [float(t0), 0, cand_sect[t0], sum(c) / len(c)] + c
            + [self.lcs[cand_sect[t0]].meta[k] for k in meta_keys]
            + [cand_pred[t0][k] for k in inj_keys]
        ) for t0, c in cand_time.iteritems() if len(c) > 1], dtype=dtype)

        # If no candidates were found, return a blank.
        if not len(candidates):
            return candidates

        # Iterate through the candidates and exclude points that overlap
        # within the window.
        m = np.ones(len(candidates), dtype=bool)
        final = np.zeros(len(candidates), dtype=bool)
        while m.sum():
            i = np.arange(len(m))[m][np.argmax(candidates[m]["mean_factor"])]
            t0 = candidates[i]["time"]
            m0 = np.abs(candidates["time"] - t0) < window
            candidates[i]["num_points"] = m0.sum()
            m[m0] = False
            final[i] = True
        return candidates[final]

    def to_hdf(self, fn):
        fn = os.path.abspath(fn)
        try:
            os.makedirs(os.path.dirname(fn))
        except os.error:
            pass

        with h5py.File(fn, "w") as f:
            for k, v in self.kwargs.iteritems():
                f.attrs[k] = v

            for results in self.models:
                if results is None:
                    continue

                # Create the data group and save the attributes.
                sect = results["split_id"]
                g = f.create_group("section_{0:03d}".format(sect))
                g.attrs["split_id"] = sect
                g.attrs["classifier"] = pickle.dumps(results["classifier"])

                # Loop over validation sets and save the data.
                for i, d0 in enumerate(results["validation"]):
                    id_ = d0["split_id"]
                    g0 = g.create_group("validation_{0:d}".format(id_))
                    for k in ["split_id", "prec_req", "threshold",
                              "area_under_the_prc"]:
                        g0.attrs[k] = d0[k]
                    for k in ["precision_recall_curve", "validation_set",
                              "validation_pred"]:
                        g0.create_dataset(k, data=d0[k], compression="gzip")

                # Loop over test sets and save the data.
                for i, d0 in enumerate(results["test"]):
                    id_ = d0["split_id"]
                    g0 = g.create_group("test_{0:d}".format(id_))
                    g0.attrs["split_id"] = id_
                    g0.create_dataset("prediction", data=d0["prediction"],
                                      compression="gzip")

    @classmethod
    def from_hdf(cls, fn, lcs, **kwargs):
        self = cls(lcs, **kwargs)
        with h5py.File(fn, "r") as f:
            for k in f.attrs:
                self.kwargs[k] = f.attrs[k]

            for k in f:
                g = f[k]

                # Create the data group and save the attributes.
                d = dict(
                    split_id=g.attrs["split_id"],
                    classifier=pickle.loads(g.attrs["classifier"]),
                    validation=[],
                    test=[],
                )

                # Loop over the groups and load the datasets.
                for k0 in g:
                    g0 = g[k0]
                    if k0.startswith("validation"):
                        d0 = dict((i, g0.attrs[i])
                                  for i in ["split_id", "prec_req",
                                            "threshold", "area_under_the_prc"])
                        for i in ["precision_recall_curve", "validation_set",
                                  "validation_pred"]:
                            d0[i] = g0[i][...]
                        d["validation"].append(d0)
                    elif k0.startswith("test"):
                        d0 = dict(split_id=g0.attrs["split_id"])
                        d0["prediction"] = g0["prediction"][...]
                        d["test"].append(d0)

                self.models[d["split_id"]] = d

        return self

    def normalize_inputs(self, X):
        if self.wavelet is None:
            t = np.arange(X.shape[1])
            for i, x in enumerate(X):
                m = np.isfinite(x)
                x[~m] = np.interp(t[~m], t[m], x[m])
                X[i, :] = np.log(x) - np.log(np.median(x))
        else:
            assert False, "WAVELETS ARE BROKEN"
            X /= np.median(X, axis=1)[:, None]
            for i, x in enumerate(X):
                X[i, :] = np.concatenate(pywt.dwt(x - 1.0, self.wavelet))[:-1]
        return X

    def unwrap_lc(self, lc):
        hw = self.half_width
        two_hw = 2*hw
        t = lc.time[np.arange(hw, len(lc)-hw)]
        f = lc.flux[np.arange(len(lc)-two_hw)[:, None]+np.arange(two_hw+1)]
        return t, f


def simulation_system(smass, srad, q1, q2, period, t0, rp, b, e, pomega):
    s = transit.System(transit.Central(mass=smass, radius=srad, q1=q1, q2=q2))
    s.add_body(transit.Body(period=period, t0=t0, r=rp, b=b, e=e,
                            pomega=pomega))
    return s
