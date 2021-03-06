# -*- coding: utf-8 -*-

from __future__ import division, print_function

__all__ = ["load_light_curves_for_kic", "load_light_curves"]

import os
import fitsio
import logging
import requests
import numpy as np
from scipy.ndimage.measurements import label as contig_label

from .catalogs import KOICatalog
from .settings import TEXP, PEERLESS_DATA_DIR


def load_light_curves_for_kic(kicid, clobber=False, remove_kois=True, **kwargs):
    # Make sure that that data directory exists.
    bp = os.path.join(PEERLESS_DATA_DIR, "data")
    try:
        os.makedirs(bp)
    except os.error:
        pass

    # Get the list of data URLs.
    urls = _get_mast_light_curve_urls(kicid)

    # Loop over the URLs and download the files if needed.
    fns = []
    for url in urls:
        fn = os.path.join(bp, url.split("/")[-1])
        fns.append(fn)
        if os.path.exists(fn) and not clobber:
            continue

        # Download the file.
        r = requests.get(url)
        if r.status_code != requests.codes.ok:
            r.raise_for_status()
        with open(fn, "wb") as f:
            f.write(r.content)

    # Load the light curves.
    if remove_kois:
        kwargs["remove_kois"] = kicid
    return load_light_curves(fns, **kwargs)


def load_light_curves(fns, pdc=True, min_break=10, delete=False,
                      remove_kois=None, downsample=1):
    # Find any KOIs.
    if remove_kois is not None:
        df = KOICatalog().df
        kois = df[df.kepid == remove_kois]
        if len(kois):
            logging.info("Removing {0} known KOIs".format(len(kois)))

    # Load the light curves.
    lcs = []
    for fn in fns:
        # Load the data.
        data, hdr = fitsio.read(fn, header=True)
        texp = hdr["INT_TIME"] * hdr["NUM_FRM"] / (24. * 60. * 60.)
        x = data["TIME"]
        q = data["SAP_QUALITY"]
        if pdc:
            y = data["PDCSAP_FLUX"]
            yerr = data["PDCSAP_FLUX_ERR"]
        else:
            y = data["SAP_FLUX"]
            yerr = data["SAP_FLUX_ERR"]

        # Compute the median error bar.
        yerr = np.median(yerr[np.isfinite(yerr)])

        # Resample the time series.
        if downsample > 1:
            # Reshape the arrays to downsample.
            downsample = int(downsample)
            l = len(x) // downsample * downsample
            inds = np.arange(l).reshape((-1, downsample))
            x, y, q = x[inds], y[inds], q[inds]

            # Ignore missing points.
            m = np.isfinite(y) & np.isfinite(x) & (q == 0)
            x[~m] = 0.0
            y[~m] = 0.0
            x = np.sum(x, axis=1)
            y = np.sum(y, axis=1)
            q = np.min(q, axis=1)

            # Take the mean.
            norm = np.sum(m, axis=1)
            m = norm > 0.0
            x[m] /= norm[m]
            x[~m] = np.nan
            y[m] /= norm[m]
            y[~m] = np.nan

            # Update the exposure time.
            texp = downsample * texp

        # Load the meta data.
        hdr = fitsio.read_header(fn, 0)
        meta = dict(
            channel=hdr["CHANNEL"],
            skygroup=hdr["SKYGROUP"],
            module=hdr["MODULE"],
            output=hdr["OUTPUT"],
            quarter=hdr["QUARTER"],
            season=hdr["SEASON"],
        )

        # Remove any KOI points.
        if remove_kois is not None:
            for _, koi in kois.iterrows():
                period = float(koi.koi_period)
                t0 = float(koi.koi_time0bk) % period
                tau = float(koi.koi_duration) / 24.
                m = np.abs((x-t0+0.5*period) % period-0.5*period) < tau
                y[m] = np.nan

        # Remove bad quality points.
        y[q != 0] = np.nan

        # Find and flag long sections of missing NaNs.
        lbls, count = contig_label(~np.isfinite(y))
        for i in range(count):
            m = lbls == i+1
            # Label sections of missing fluxes longer than min_break points
            # by setting the times equal to NaN.
            if m.sum() > min_break:
                x[m] = np.nan

        # Split into months.
        m = np.isfinite(x)
        gi = np.arange(len(x))[m]
        bi = np.arange(len(x))[~m]
        if len(bi):
            bi = bi[(bi > gi[0]) & (bi < gi[-1])]
            d = np.diff(bi)
            chunks = [slice(gi[0], bi[0])]
            for a, b in zip(bi[:-1][d > 1], bi[1:][d > 1]):
                chunks.append(slice(a+1, b-1))
            chunks.append(slice(bi[-1]+1, gi[-1]))
        else:
            chunks = [slice(gi[0], gi[-1])]

        # Interpolate missing data.
        for c in chunks:
            x0, y0 = x[c], y[c]
            m = np.isfinite(y0)
            if not np.any(m):
                continue
            # y0[~m] = np.interp(x0[~m], x0[m], y0[m])
            # y0[~m] += yerr * np.random.randn((~m).sum())
            lcs.append(LightCurve(x0, y0, yerr, meta, texp=texp))

        if delete:
            os.remove(fn)
    return lcs


class LightCurve(object):

    def __init__(self, time, flux, yerr, meta, texp=TEXP):
        self.time = np.ascontiguousarray(time, dtype=float)
        mu = np.median(flux)
        self.flux = np.ascontiguousarray(flux / mu, dtype=float)
        self.yerr = float(yerr) / mu
        self.meta = meta
        self.footprint = self.time.max() - self.time.min()
        self.texp = texp

    def __len__(self):
        return len(self.time)


def _get_mast_light_curve_urls(kic, short_cadence=False, **params):
    # Build the URL and request parameters.
    url = "http://archive.stsci.edu/kepler/data_search/search.php"
    params["action"] = params.get("action", "Search")
    params["outputformat"] = "JSON"
    params["coordformat"] = "dec"
    params["verb"] = 3
    params["ktc_kepler_id"] = kic
    params["ordercolumn1"] = "sci_data_quarter"
    if not short_cadence:
        params["ktc_target_type"] = "LC"

    # Get the list of files.
    r = requests.get(url, params=params)
    if r.status_code != requests.codes.ok:
        r.raise_for_status()

    # Format the data URLs.
    kic = "{0:09d}".format(kic)
    base_url = ("http://archive.stsci.edu/pub/kepler/lightcurves/{0}/{1}/"
                .format(kic[:4], kic))
    for row in r.json():
        ds = row["Dataset Name"].lower()
        tt = row["Target Type"].lower()
        yield base_url + "{0}_{1}lc.fits".format(ds, tt[0])
