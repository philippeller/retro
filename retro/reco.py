#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=wrong-import-position, redefined-outer-name, range-builtin-not-iterating, too-many-locals

"""
Instantiate Retro tables and find the max over the log-likelihood space.
"""

from __future__ import absolute_import, division, print_function

__all__ = ['RetroReco', 'parse_args']

__author__ = 'J.L. Lanfranchi, P. Eller'
__license__ = '''Copyright 2017 Justin L. Lanfranchi

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.'''

from argparse import ArgumentParser
from collections import OrderedDict

from os.path import abspath, dirname, join
import pickle
import sys
import time

import numpy as np
from scipy import stats

if __name__ == '__main__' and __package__ is None:
    RETRO_DIR = dirname(dirname(abspath(__file__)))
    if RETRO_DIR not in sys.path:
        sys.path.append(RETRO_DIR)
from retro import init_obj
from retro.const import PEGLEG_PARAM_NAMES, SCALING_PARAM_NAMES
from retro.retro_types import EVT_DOM_INFO_T, EVT_HIT_INFO_T
from retro.utils.geom import rotate_point, add_vectors
from retro.utils.misc import expand, mkdir, sort_dict
from retro.utils.stats import estimate_from_llhp
from retro.priors import (
    get_prior_def,
    get_prior_fun,
    PRI_UNIFORM,
    PRI_LOG_NORMAL,
    PRI_LOG_UNIFORM,
)
from retro.hypo.discrete_muon_kernels import pegleg_eval

report_after = 100

class RetroReco(object):

    def __init__(self, dom_tables_kw, hypo_kw, events_kw, reco_kw):
        self.dom_tables = init_obj.setup_dom_tables(**dom_tables_kw)

        # check tables are finite:
        for tbl in self.dom_tables.tables:
            assert np.sum(~np.isfinite(tbl['weight'])) == 0, 'table not finite!'
            assert np.sum(tbl['weight'] < 0) == 0, 'table is negative!'
            assert np.min(tbl['index']) >= 0, 'table has negative index'
            assert np.max(tbl['index']) < self.dom_tables.template_library.shape[0], 'table too large index'
        assert np.sum(~np.isfinite(self.dom_tables.template_library)) == 0, 'templates not finite!'
        assert np.sum(self.dom_tables.template_library < 0) == 0, 'templates not finite!'

        self.hypo_handler = init_obj.setup_discrete_hypo(**hypo_kw)
        self.events_iterator = init_obj.get_events(**events_kw)
        self.reco_kw = reco_kw

        self.n_params = self.hypo_handler.n_params
        self.n_opt_params = self.hypo_handler.n_opt_params

        self.out_prefix = None

        # Setup priors
        self.prior_defs = OrderedDict()
        for param in self.hypo_handler.opt_param_names:
            self.prior_defs[param] = get_prior_def(param, reco_kw)

        # Remove unused reco kwargs
        reco_kw.pop('spatial_prior')
        reco_kw.pop('cascade_angle_prior')

    def run(self, method):
        """Run reconstructions.

        Parameters
        ----------
        method : str
            One of "multinest", "nlopt", "scipy", or "skopt".

        """
        print('Running reconstructions...')
        t00 = time.time()
        outdir = self.reco_kw.pop('outdir')
        outdir = expand(outdir)
        mkdir(outdir)

        # Setup LLHP dtype
        dim_names = list(self.hypo_handler.all_param_names)

        # add derived quantities
        derived_dim_names = ['energy', 'azimuth', 'zenith']
        if 'cascade_d_zenith' in dim_names and 'cascade_d_azimuth' in dim_names:
            derived_dim_names += ['cascade_zenith', 'cascade_azimuth']

        all_dim_names = dim_names + derived_dim_names

        llhp_t = np.dtype([(field, np.float32) for field in ['llh'] + all_dim_names])

        for event_idx, event in self.events_iterator:
            t0 = time.time()

            self.out_prefix = join(outdir, 'evt{}-'.format(event_idx))
            print('Output files prefix: "{}"\n'.format(self.out_prefix))

            prior, priors_used = self.generate_prior(event)
            param_values = []
            log_likelihoods = []
            t_start = []
            loglike = self.generate_loglike(
                event=event,
                param_values=param_values,
                log_likelihoods=log_likelihoods,
                t_start=t_start,
            )

            if method == 'test':
                settings = self.run_test(
                    prior=prior,
                    loglike=loglike,
                )

            if method == 'truth':
                settings = self.run_with_truth(
                    prior=prior,
                    loglike=loglike,
                    truth=event['truth'],
                )

            elif method == 'mymini':
                settings = self.run_mymini(
                    prior=prior,
                    loglike=loglike,
                )

            elif method == 'multinest':
                settings = self.run_multinest(
                    prior=prior,
                    loglike=loglike,
                    **self.reco_kw
                )
            elif method == 'scipy':
                settings = self.run_scipy(
                    prior=prior,
                    loglike=loglike,
                    method='differential_evolution',
                    eps=0.02
                )
            elif method == 'nlopt':
                settings = self.run_nlopt(
                    prior=prior,
                    loglike=loglike,
                )
            elif method == 'skopt':
                settings = self.run_skopt(
                    prior=prior,
                    loglike=loglike,
                )
            else:
                raise ValueError('Unknown `Method` {}'.format(method))

            t1 = time.time()

            # dump
            llhp = np.zeros(shape=len(param_values), dtype=llhp_t)
            llhp['llh'] = log_likelihoods
            llhp[dim_names] = param_values

            
            # create derived dimensions
            if 'energy' in derived_dim_names:
                if 'track_energy' in dim_names:
                    llhp['energy'] += llhp['track_energy']
                if 'cascade_energy' in dim_names:
                    llhp['energy'] += llhp['cascade_energy']

            if 'cascade_d_zenith' in dim_names and 'cascade_d_azimuth' in dim_names:
                # create cascade angles from delta angles
                rotate_point(llhp['cascade_d_zenith'], llhp['cascade_d_azimuth'],
                             llhp['track_zenith'], llhp['track_azimuth'],
                             llhp['cascade_zenith'], llhp['cascade_azimuth'])

            if 'track_zenith' in all_dim_names and 'track_azimuth' in all_dim_names:
                if 'cascade_zenith' in all_dim_names and 'cascade_azimuth' in all_dim_names:
                    # this resulting radius we won't need, but need to supply an array to the function
                    r_out = np.empty(shape=llhp.shape, dtype=np.float32)
                    # combine angles:
                    add_vectors(llhp['track_energy'], llhp['track_zenith'], llhp['track_azimuth'],
                                llhp['cascade_energy'], llhp['cascade_zenith'], llhp['cascade_azimuth'],
                                r_out, llhp['zenith'], llhp['azimuth'])

                else:
                    # in this case there is no cascade angles
                    llhp['zenith'] = llhp['track_zenith']
                    llhp['azimuth'] = llhp['track_azimuth']

            elif 'cascade_zenith' in all_dim_names and 'cascade_azimuth' in all_dim_names:
                # in this case there are no track angles
                llhp['zenith'] = llhp['cascade_zenith']
                llhp['azimuth'] = llhp['cascade_azimuth']


            if False:
                # save full info
                llhp_outf = self.out_prefix + 'llhp.npy'
                print('Saving llhp to "{}"...'.format(llhp_outf))
                np.save(llhp_outf, llhp)

            opt_meta = OrderedDict([
                ('params', dim_names),
                ('original_prior_specs', self.prior_defs),
                ('priors_used', priors_used),
                ('settings', sort_dict(settings)),
            ])

            opt_meta['num_llhp'] = len(param_values)
            opt_meta['run_time'] = t1 - t0

            opt_meta_outf = self.out_prefix + 'opt_meta.pkl'
            print('Saving metadata to "{}"'.format(opt_meta_outf))
            pickle.dump(
                opt_meta,
                open(opt_meta_outf, 'wb'),
                protocol=pickle.HIGHEST_PROTOCOL
            )

            if False:
                dt = time.time() - t1
                n_points = llhp.size
                print('  ---> {:.3f} s, {:d} points ({:.3f} ms per LLH)'
                      .format(dt, n_points, dt / n_points * 1e3))

            #estimate = estimate_from_llhp(llhp, opt_meta)
            estimate = estimate_from_llhp(llhp)
            estimate_outf = self.out_prefix + 'estimate.pkl'
            print('Saving estimate to "{}"'.format(estimate_outf))
            pickle.dump(
                estimate,
                open(estimate_outf, 'wb'),
                protocol=pickle.HIGHEST_PROTOCOL
            )


        print('Total script run time is {:.3f} s'.format(time.time() - t00))

    def generate_prior(self, event):
        """Generate the prior transform functions + info for a given event.

        Parameters
        ----------
        event

        Returns
        -------
        prior : callable
        priors_used : OrderedDict

        """
        prior_funcs = []
        priors_used = OrderedDict()

        for dim_num, dim_name in enumerate(self.hypo_handler.all_param_names):
            if dim_name in PEGLEG_PARAM_NAMES + SCALING_PARAM_NAMES:
                continue
            prior_fun, prior_def = get_prior_fun(
                dim_num=dim_num,
                dim_name=dim_name,
                prior_def=self.prior_defs[dim_name],
                event=event,
            )
            prior_funcs.append(prior_fun)
            priors_used[dim_name] = prior_def

        def prior(cube, ndim=None, nparams=None): # pylint: disable=unused-argument
            """Apply `prior_funcs` to the hypercube to map values from the unit
            hypercube onto values in the physical parameter space.

            The result overwrites the values in `cube`.

            Parameters
            ----------
            cube
            ndim
            nparams

            """
            for prior_func in prior_funcs:
                prior_func(cube)
            #print(cube)
            #print([cube[i] for i in range(6)])

        return prior, priors_used

    def generate_loglike(self, event, param_values, log_likelihoods, t_start):
        """Generate the LLH callback function for a given event

        Parameters
        ----------
        event
        param_values : list
        log_likelihoods : list
        t_start : list

        Returns
        -------
        loglike : callable

        """
        # -- Variables to be captured by `loglike` closure -- #


        all_param_names = self.hypo_handler.all_param_names
        n_opt_params = self.n_opt_params

        hits = event['hits']
        hits_indexer = event['hits_indexer']
        hypo_handler = self.hypo_handler
        pegleg_muon_dt = hypo_handler.pegleg_kernel_kwargs.get('dt')
        pegleg_muon_const_e_loss = False

        get_llh = self.dom_tables._get_llh # pylint: disable=protected-access
        dom_info = self.dom_tables.dom_info
        tables = self.dom_tables.tables
        table_norms = self.dom_tables.table_norms
        t_indep_tables = self.dom_tables.t_indep_tables
        t_indep_table_norms = self.dom_tables.t_indep_table_norms
        sd_idx_table_indexer = self.dom_tables.sd_idx_table_indexer

        truth_info = OrderedDict()
        truth_info['x'] = event['truth']['x']
        truth_info['y'] = event['truth']['y']
        truth_info['z'] = event['truth']['z']
        truth_info['time'] = event['truth']['time']
        truth_info['zenith'] = np.arccos(event['truth']['coszen'])
        truth_info['azimuth'] = event['truth']['azimuth']
        truth_info['track_azimuth'] = event['truth']['longest_daughter_azimuth']
        truth_info['track_zenith'] = np.arccos(event['truth']['longest_daughter_coszen'])
        truth_info['track_energy'] = event['truth']['longest_daughter_energy']
        truth_info['cascade_azimuth'] = event['truth']['cascade_azimuth']
        truth_info['cascade_zenith'] = np.arccos(event['truth']['cascade_coszen'])
        truth_info['cascade_energy'] = event['truth']['cascade_energy']
        truth_info['neutrino_energy'] = event['truth']['energy']

        num_operational_doms = np.sum(dom_info['operational'])

        # Array containing only DOMs operational during the event & info
        # relevant to the hits these DOMs got (if any)
        event_dom_info = np.zeros(shape=num_operational_doms, dtype=EVT_DOM_INFO_T)

        # Array containing all relevant hit info for the event, including a
        # pointer back to the index of the DOM in the `event_dom_info` array
        event_hit_info = np.zeros(shape=hits.size, dtype=EVT_HIT_INFO_T)

        # Copy 'time' and 'charge' over directly; add 'event_dom_idx' below
        event_hit_info[['time', 'charge']] = hits[['time', 'charge']]

        copy_fields = ['sd_idx', 'x', 'y', 'z', 'quantum_efficiency', 'noise_rate_per_ns']

        print('all noise rate %.5f'%np.sum(dom_info['noise_rate_per_ns']))
        print('DOMs with zero noise %i'%np.sum(dom_info['noise_rate_per_ns'] == 0))

        # Fill `event_{hit,dom}_info` arrays only for operational DOMs
        for dom_idx, this_dom_info in enumerate(dom_info[dom_info['operational']]):
            this_event_dom_info = event_dom_info[dom_idx:dom_idx+1]
            this_event_dom_info[copy_fields] = this_dom_info[copy_fields]
            sd_idx = this_dom_info['sd_idx']
            this_event_dom_info['table_idx'] = sd_idx_table_indexer[sd_idx]

            # Copy any hit info from `hits_indexer` and total charge from
            # `hits` into `event_hit_info` and `event_dom_info` arrays
            this_hits_indexer = hits_indexer[hits_indexer['sd_idx'] == sd_idx]
            if len(this_hits_indexer) == 0:
                this_event_dom_info['hits_start_idx'] = 0
                this_event_dom_info['hits_stop_idx'] = 0
                this_event_dom_info['total_observed_charge'] = 0
                continue

            start = this_hits_indexer[0]['offset']
            stop = start + this_hits_indexer[0]['num']
            event_hit_info[start:stop]['event_dom_idx'] = dom_idx
            this_event_dom_info['hits_start_idx'] = start
            this_event_dom_info['hits_stop_idx'] = stop
            this_event_dom_info['total_observed_charge'] = (
                np.sum(hits[start:stop]['charge'])
            )

        print('this evt. noise rate %.5f'%np.sum(event_dom_info['noise_rate_per_ns']))
        print('DOMs with zero noise: %i'%np.sum(event_dom_info['noise_rate_per_ns'] == 0))
        # settings those to minimum noise
        noise = event_dom_info['noise_rate_per_ns']
        mask = noise < 1e-7
        noise[mask] = 1e-7
        print('this evt. noise rate %.5f'%np.sum(event_dom_info['noise_rate_per_ns']))
        print('DOMs with zero noise: %i'%np.sum(event_dom_info['noise_rate_per_ns'] == 0))
        print('min noise: ', np.min(noise))
        print('mean noise: ', np.mean(noise))

        assert np.sum(event_dom_info['quantum_efficiency'] <= 0) == 0, 'negative QE'
        assert np.sum(event_dom_info['total_observed_charge']) > 0, 'no charge'
        assert np.isfinite(np.sum(event_dom_info['total_observed_charge'])), 'inf charge'


        def loglike(cube, ndim=None, nparams=None): # pylint: disable=unused-argument
            """Get log likelihood values.

            Defined as a closure to capture particulars of the event and priors without
            having to pass these as parameters to the function.

            Note that this is called _after_ `prior` has been called, so `cube`
            already contains the parameter values scaled to be in their physical
            ranges.

            Parameters
            ----------
            cube
            ndim : int, optional
            nparams : int, optional

            Returns
            -------
            llh : float

            """
            t0 = time.time()
            if not t_start:
                t_start.append(time.time())

            hypo = OrderedDict(list(zip(all_param_names, cube)))

            generic_sources = hypo_handler.get_generic_sources(hypo)
            pegleg_sources = hypo_handler.get_pegleg_sources(hypo)
            scaling_sources = hypo_handler.get_scaling_sources(hypo)

            llh, pegleg_idx, scalefactor = get_llh(
                generic_sources=generic_sources,
                pegleg_sources=pegleg_sources,
                scaling_sources=scaling_sources,
                event_hit_info=event_hit_info,
                event_dom_info=event_dom_info,
                tables=tables,
                table_norms=table_norms,
                t_indep_tables=t_indep_tables,
                t_indep_table_norms=t_indep_table_norms,
                pegleg_stepsize=1,
            )
            assert np.isfinite(llh), 'LLH not finite'
            assert llh < 0, 'LLH positive'

            pegleg_result = pegleg_eval(
                pegleg_idx=pegleg_idx,
                dt=pegleg_muon_dt,
                const_e_loss=pegleg_muon_const_e_loss,
            )
            result = tuple(cube[:n_opt_params]) + (pegleg_result, scalefactor)
            param_values.append(result)

            log_likelihoods.append(llh)
            n_calls = len(log_likelihoods)
            t1 = time.time()

            if n_calls % report_after == 0:
                print('')
                msg = 'truth:                '
                for key, val in zip(all_param_names, result):
                    try:
                        msg += ' %s=%.1f'%(key, truth_info[key])
                    except KeyError:
                        pass
                print(msg)
                t_now = time.time()
                best_idx = np.argmax(log_likelihoods)
                best_llh = log_likelihoods[best_idx]
                best_p = param_values[best_idx]
                msg = 'best llh = {:.3f} @ '.format(best_llh)
                for key, val in zip(all_param_names, best_p):
                    msg += ' %s=%.1f'%(key, val)
                print(msg)
                msg = 'this llh = {:.3f} @ '.format(llh)
                for key, val in zip(all_param_names, result):
                    msg += ' %s=%.1f'%(key, val)
                print(msg)
                print('{} LLH computed'.format(n_calls))
                print('avg time per llh: {:.3f} ms'.format((t_now - t_start[0])/n_calls*1000))
                print('this llh took:    {:.3f} ms'.format((t1 - t0)*1000))
                print('')

            return llh

        return loglike

    def run_test(self, prior, loglike):
        '''
        Random sampling instead of an actual minimizer
        '''
        rand = np.random.RandomState()
        for i in range(100):
            param_vals = rand.uniform(0,1,self.n_opt_params)
            prior(param_vals)
            llh = loglike(param_vals)
        return OrderedDict()

    def run_with_truth(self, prior, loglike, truth):
        true_params = np.zeros(self.n_opt_params)

        for i,name in enumerate(self.hypo_handler.opt_param_names):
            if name in ['x', 'y', 'z', 'time']:
                true_params[i] = truth[name]
            elif name == 'track_zenith':
                true_params[i] = np.arccos(truth['coszen'])
            elif name == 'track_azimuth':
                true_params[i] = truth['azimuth']
            else:
                raise NotImplementedError()
        # now random values for these dimension
        rand_dims = []
        #rand_dims = [4,5]
        rand = np.random.RandomState()
        if len(rand_dims) > 1:
            for i in range(10000):
                rand_params = rand.uniform(0,1,self.n_opt_params)
                prior(rand_params)
                param_vals = np.zeros(self.n_opt_params)
                param_vals[:] = true_params[:]
                param_vals[rand_dims] = rand_params[rand_dims]
                llh = loglike(param_vals)
        else:
            llh = loglike(true_params)


        return OrderedDict()

    def run_mymini(self, prior, loglike):
        '''
        Implementation of the CRS2 algoriyhm with local mutation as described in
        JOURNAL OF OPTIMIZATION THEORY AND APPLICATIONS: Vol. 130, No. 2, pp. 253–264,
        August 2006 (C 2006) DOI: 10.1007/s10957-006-9101-0

        Adapted to work with spherical corrdinates (correct centroid calculation, reflection and mutation)

        At the moment the number of cartesian (standard) parameters `n_cart` and spherical parameters `n_spher` is hard coded
        Furthermore, all cartesian coordinates must come first followed by `azimuth_1, zenith_1, azimuth_2, zenith_2, ...`

        '''
        rand = np.random.RandomState()

        # if true: use priors (for cartesian coordinates only) during minimization
        # if false: use priors only to generate initial population, then perform minimization on actual parameter values

        from sobol import i4_sobol

        use_priors = False

        def fun(x): 
            '''
            callable for minimizer
            '''
            if use_priors:
                param_vals = np.zeros_like(x)
                param_vals[:n_cart] = x[:n_cart]
                prior(param_vals)
                param_vals[n_cart:] = x[n_cart:]
            else:
                param_vals = x
            llh = loglike(param_vals)
            return -llh

        n = self.n_opt_params
        n_cart = 4
        n_spher = 1

        # type to store spherical coordinates and handy quantities
        spher_cord = np.dtype([('zen',np.float32),
                               ('az', np.float32),
                               ('x', np.float32),
                               ('y', np.float32),
                               ('z', np.float32),
                               ('sinzen', np.float32),
                               ('coszen', np.float32),
                               ('sinaz', np.float32),
                               ('cosaz', np.float32),
                               ])

        def create_x(x_cart, x_spher):
            '''
            patch back together the cartesian and spherical coordinates into one array
            '''
            # ToDO: make proper
            x = np.empty(n)
            x[:n_cart] = x_cart
            x[n_cart+1::2] = x_spher['zen']
            x[n_cart::2] = x_spher['az']
            return x

        def fill_from_spher(s):
            '''
            fill in the remaining values giving the two angles `zen` and `az`
            '''
            s['sinzen'] = np.sin(s['zen'])
            s['coszen'] = np.cos(s['zen'])
            s['sinaz'] = np.sin(s['az'])
            s['cosaz'] = np.cos(s['az'])
            s['x'] = s['sinzen'] * s['cosaz']
            s['y'] = s['sinzen'] * s['sinaz']
            s['z'] = s['coszen']

        def fill_from_cart(s):
            '''
            fill in the remaining values giving the cart, coords. `x`, `y` and `z`
            '''
            radius = np.sqrt(s['x']**2 + s['y']**2 + s['z']**2)

            if not radius == 0:
                # make sure they're length 1
                s['x'] /= radius
                s['y'] /= radius
                s['z'] /= radius
                s['az'] = np.arctan2(s['y'], s['x']) % (2 * np.pi)
                s['coszen'] = s['z']
                s['zen'] = np.arccos(s['coszen'])
                s['sinzen'] = np.sin(s['zen'])
                s['sinaz'] = np.sin(s['az'])
                s['cosaz'] = np.cos(s['az'])
            else:
                s['z'] = 1
                s['az'] = 0
                s['zen'] = 0
                s['coszen'] = 1
                s['sinzen'] = 0
                s['cosaz'] = 1
                s['sinaz'] = 0


        def reflect(old, centroid, new):
            '''
            reflect the old point around the centroid into the new point on the sphere
            '''

            x = old['x']
            y = old['y']
            z = old['z']

            ca = centroid['cosaz']
            sa = centroid['sinaz']
            cz = centroid['coszen']
            sz = centroid['sinzen']
            
            new['x'] = 2*ca*cz*sz*z + x*(ca*(-ca*cz**2 + ca*sz**2) - sa**2) + y*(ca*sa + sa*(-ca*cz**2 + ca*sz**2))
            new['y'] = 2*cz*sa*sz*z + x*(ca*sa + ca*(-cz**2*sa + sa*sz**2)) + y*(-ca**2 + sa*(-cz**2*sa + sa*sz**2))
            new['z'] = 2*ca*cz*sz*x + 2*cz*sa*sz*y + z*(cz**2 - sz**2)

            fill_from_cart(new)

        #N = 10 * (n + 1)
        N = 160
        assert N > n + 1

        # that many more initial individuals (didn;t seem to help realy)
        initial_factor = 1

        S_cart = np.zeros(shape=(N*initial_factor,n_cart))
        S_spher = np.zeros(shape=(N*initial_factor,n_spher), dtype=spher_cord)
        fx = np.zeros(shape=(N*initial_factor,))


        # initial population
        for i in range(N*initial_factor):
            #x = rand.uniform(0,1,n)
            x, _ = i4_sobol(n, i+1)
            param_vals = np.copy(x)
            prior(param_vals)
            # always transform angles!
            x[n_cart:] = param_vals[n_cart:]
            if not use_priors:
                x[:n_cart] = param_vals[:n_cart]

            # break up into cartesiand and spherical coordinates
            # ToDO: make proper
            S_cart[i] = x[:n_cart]
            S_spher[i]['zen'] = x[n_cart+1::2]
            S_spher[i]['az'] = x[n_cart::2]
            fill_from_spher(S_spher[i])
            fx[i] = fun(x)


        if initial_factor > 1:
            # cut down to best N
            mask = fx <= np.percentile(fx, 100./initial_factor)
            S_cart = S_cart[mask]
            S_spher = S_spher[mask]
            fx = fx[mask]


        best_llh = np.min(fx)
        no_improvement_counter = -1

        #for k in range(5):

        simplex_success = 0
        mutation_success = 0
        whateverido = 0
        failure = 0
        for k in range(50000):

            if k % report_after == 0:
                print('simplex: %i, mutation: %i, mine: %i, failed: %i'%(simplex_success, mutation_success, whateverido, failure))

            # minimizer loop

            # break condition
            #print(np.std(fx))
            if np.std(fx) < 0.1:
                print('std below threshold, stopping.')
                break

            if no_improvement_counter > 2000:
                print('no improvement found, stopping.')
                break

            new_best_llh = np.min(fx)
            if new_best_llh < best_llh:
                best_llh = new_best_llh
                no_improvement_counter = 0
            else:
                no_improvement_counter += 1

            worst_idx = np.argmax(fx)
            best_idx = np.argmin(fx)

            # choose n random points but not best
            choice = rand.choice(N-1, n, replace=False)
            choice[choice >= best_idx] +=1

            # maybe we'll need to actualy reflect the worst point in the simplex?
            # or choose random, not last?
            # also, not necessary to exclude worst point from simplex?

            # cardtesian centroid
            centroid_cart = (np.sum(S_cart[choice[:-1]], axis=0) + S_cart[best_idx]) / n
            # reflect point
            new_x_cart = 2*centroid_cart - S_cart[choice[-1]]

            # spherical centroid
            centroid_spher = np.zeros(n_spher, dtype=spher_cord)
            centroid_spher['x'] = (np.sum(S_spher['x'][choice[:-1]], axis=0) + S_spher['x'][best_idx]) / n
            centroid_spher['y'] = (np.sum(S_spher['y'][choice[:-1]], axis=0) + S_spher['y'][best_idx]) / n
            centroid_spher['z'] = (np.sum(S_spher['z'][choice[:-1]], axis=0) + S_spher['z'][best_idx]) / n
            fill_from_cart(centroid_spher)
            # reflect point
            new_x_spher = np.zeros(n_spher, dtype=spher_cord)
            reflect(S_spher[choice[-1]], centroid_spher, new_x_spher)

            if use_priors:
                outside = np.any(new_x_cart < 0) or np.any(new_x_cart > 1)
            else:
                outside = False

            if not outside:
                new_fx = fun(create_x(new_x_cart, new_x_spher))

                if new_fx < fx[worst_idx]:
                    # found better point
                    S_cart[worst_idx] = new_x_cart
                    S_spher[worst_idx] = new_x_spher
                    fx[worst_idx] = new_fx
                    simplex_success += 1
                    continue

            '''
            # mutation
            #w = rand.uniform(0, 1, n_cart)
            w = rand.uniform(-0.5, 1.5, n_cart)
            new_x_cart2 = (1 + w) * S_cart[best_idx] - w * new_x_cart

            # first reflect at best point
            reflected_new_x_spher = np.zeros(n_spher, dtype=spher_cord)
            reflect(new_x_spher, S_spher[best_idx], reflected_new_x_spher)

            new_x_spher2 = np.zeros_like(new_x_spher)

            # now do a combination of best and reflected point with weight w
            for dim in ['x', 'y', 'z']:
                w = rand.uniform(-0.5, 1.5, n_spher)
                #w = rand.uniform(0, 1, n_spher)
                new_x_spher2[dim] = (1 - w) * S_spher[best_idx][dim] + w * reflected_new_x_spher[dim]
            fill_from_cart(new_x_spher2)

            if use_priors:
                outside = np.any(new_x_cart2 < 0) or np.any(new_x_cart2 > 1)
            else:
                outside = False

            if not outside:
                new_fx = fun(create_x(new_x_cart2, new_x_spher2))

                if new_fx < fx[worst_idx]:
                    #print('-> MUT accepted')
                    # found better point
                    S_cart[worst_idx] = new_x_cart2
                    S_spher[worst_idx] = new_x_spher2
                    fx[worst_idx] = new_fx
                    mutation_success += 1
                    continue

            # do own blah
            # random combination from individuals
            #for dim in range(n_cart):
            #    new_x_cart[dim] = S_cart[rand.choice(N), dim]
            #for dim in range(n_spher):
            #    new_x_spher[dim]['az'] = S_spher[rand.choice(N), dim]['az']
            #    new_x_spher[dim]['zen'] = S_spher[rand.choice(N), dim]['zen']
            #    fill_from_spher(new_x_spher)

            #    # now fuck up angles a little
            #    new_x_spher[dim]['x'] += rand.rand(1) * 2 - 1
            #    new_x_spher[dim]['y'] += rand.rand(1) * 2 - 1
            #    new_x_spher[dim]['z'] += rand.rand(1) * 2 - 1
            #    

            #new_fx = fun(create_x(new_x_cart, new_x_spher))
            ##print(new_fx, new_x_cart, new_x_spher)

            #if new_fx < fx[worst_idx]:
            #    # found better point
            #    S_cart[worst_idx] = new_x_cart
            #    S_spher[worst_idx] = new_x_spher
            #    fx[worst_idx] = new_fx
            #    whateverido += 1
            #    continue

            # skip mutation
            #continue
            # try flipping the angles
            #new_x_spher['zen'] = np.pi - new_x_spher['zen']
            #new_x_spher['az'] = (new_x_spher['az'] + np.pi) % (2 * np.pi)
            #new_fx = fun(create_x(new_x_cart, new_x_spher))

            #if new_fx < fx[worst_idx]:
            #    # found better point
            #    print('->FLIP accepted')
            #    S_cart[worst_idx] = new_x_cart
            #    S_spher[worst_idx] = new_x_spher
            #    fx[worst_idx] = new_fx
            #    continue
            '''

            
            failure += 1

        #return OrderedDict()
        
        # test a few timings:
        #print('test timing')
        #best_x_cart = np.copy(S_cart[best_idx])
        #best_x_spher = np.copy(S_spher[best_idx])

        #dt = np.arange(-40,41,5)
        #dx = - dt * best_x_spher['cosaz'] * best_x_spher['sinzen']
        #dy = - dt * best_x_spher['sinaz'] * best_x_spher['sinzen']
        #dz = - dt * best_x_spher['coszen']

        #for i in range(len(dt)):
        #    new_x_cart  = np.copy(best_x_cart)
        #    new_x_cart[0] += dt[i]
        #    new_x_cart[1] += dx[i]
        #    new_x_cart[2] += dy[i]
        #    new_x_cart[3] += dz[i]
        #    new_fx = fun(create_x(new_x_cart, best_x_spher))
        #    print(new_fx)

        return OrderedDict()

        # now let's do some sampling
        import emcee

        az_dim = 4
        zen_dim = 5

        def lnprob(new_x):
            '''
            function for sampler
            '''
            while new_x[zen_dim] < 0 or new_x[zen_dim] > np.pi:
                new_x[az_dim] += np.pi
                if new_x[zen_dim] < 0:
                    new_x[zen_dim] = -new_x[zen_dim]
                else:
                    new_x[zen_dim] = np.pi - new_x[zen_dim]

            new_x[az_dim] = new_x[az_dim] % (2 * np.pi)
            new_llh = fun(new_x)
            return -new_llh

        

        # first create array without dtype (otherwise covariance doesn't work)

        ## bigger arrays to also contain sampled points 
        S = np.zeros(shape=(N,n))
        ##f = np.full(2*N, np.inf)

        ## set the first half
        for i in range(N):
            S[i] = create_x(S_cart[i], S_spher[i])

        sampler = emcee.EnsembleSampler(N, n, lnprob)
        sampler.run_mcmc(S, 42)



        ##f[:N] = fx
        #

        #N_sampling = 10000
        #counter = 0

        #while counter < N_sampling:
        #    #print('Sampling round %i'%k)

        #    
        #    az_values = S[:,az_dim]

        #    # move the azimuths
        #    circmean = stats.circmean(az_values)
        #    if circmean > np.pi:
        #        az_values[az_values < circmean - np.pi] += (2 * np.pi)
        #    else:
        #        az_values[az_values > circmean + np.pi] -= (2 * np.pi)
        #    
        #    worst_idx = np.argmax(fx)
        #    worst_llh = fx[worst_idx]

        #    best_idx = np.argmin(fx)

        #    weights = np.exp(fx - np.max(fx))

        #    # calculate mean and covariance
        #    #mean = np.average(S, axis=0, weights=weights)
        #    #print(mean)
        #    mean = S[best_idx]
        #    cov = np.cov(S.T[:az_dim])

        #    az_values = az_values % (2 * np.pi)

        #    for j in range(100):
        #        new_x = np.zeros(n)
        #        for i in range(n):
        #            new_x[i] = rand.randn(1) * np.std(S[:,i]) + mean[i]

        #        #new_x[:az_dim] = rand.multivariate_normal(mean[:az_dim], cov, 1)[0]
        #        #new_x[az_dim] = rand.randn(1) * np.std(az_values) + mean[az_dim]
        #        #new_x[zen_dim] = rand.randn(1) * np.std(S[:,zen_dim]) + mean[zen_dim]

        #        while new_x[zen_dim] < 0 or new_x[zen_dim] > np.pi:
        #            new_x[az_dim] += np.pi
        #            if new_x[zen_dim] < 0:
        #                new_x[zen_dim] = -new_x[zen_dim]
        #            else:
        #                new_x[zen_dim] = np.pi - new_x[zen_dim]

        #        new_x[az_dim] = new_x[az_dim] % (2 * np.pi)
        #        new_llh = fun(new_x)
        #        counter += 1
        #        if new_llh < worst_llh:
        #            S[worst_idx] = new_x
        #            fx[worst_idx] = new_llh
        #            #print('found better point after %i trials'%j)
        #            break
        #        else:
        #            if j == 99:
        #                print('failed to find better point in 100 trials')

        #        
        #    # fold in zeniths and flip azimuths
        #    #zen_values = S[:,zen_dim]
        #    #az_values = S[:,az_dim]
        #    #while np.any(zen_values < 0) or np.any(zen_values > np.pi):
        #    #    az_values[zen_values < 0] += np.pi
        #    #    az_values[zen_values > np.pi] += np.pi
        #    #    zen_values[zen_values < 0] = -zen_values[zen_values < 0]
        #    #    zen_values[zen_values > np.pi] = np.pi - zen_values[zen_values > np.pi]

        #    ## make sure boundaries are ok?
        #    #az_values = az_values % (2 * np.pi)
        #    

        #    # evaluate LLH
        #    #for i in range(N,2*N):
        #    #    f[i] = fun(S[i])
        #    #
        #    ## find best half
        #    #mask = f <= np.median(f)

        #    ## reset first half
        #    #S[:N] = S[mask]
        #    #f[:N] = f[mask]


        return OrderedDict()

    def run_scipy(self, prior, loglike, method, eps):
        from scipy import optimize

        # initial guess
        x0 = 0.5 * np.ones(shape=self.n_opt_params)

        def fun(x, *args): # pylint: disable=unused-argument, missing-docstring
            param_vals = np.copy(x)
            prior(param_vals)
            llh = loglike(param_vals)
            del param_vals
            return -llh

        bounds = [(eps, 1 - eps)]*self.n_opt_params
        settings = OrderedDict()
        settings['eps'] = eps

        if method == 'differential_evolution':
            optimize.differential_evolution(fun, bounds=bounds, popsize=100)
        else:
            optimize.minimize(fun, x0, method=method, bounds=bounds, options=settings)

        return settings

    def run_skopt(self, prior, loglike):
        from skopt import gp_minimize #, forest_minimize

        # initial guess
        x0 = 0.5 * np.ones(shape=self.n_opt_params)

        def fun(x, *args): # pylint: disable=unused-argument, missing-docstring
            param_vals = np.copy(x)
            prior(param_vals)
            llh = loglike(param_vals)
            del param_vals
            return -llh

        bounds = [(0, 1)]*self.n_opt_params
        settings = OrderedDict()

        _ = gp_minimize(
            fun,                # function to minimize
            bounds,             # bounds on each dimension of x
            acq_func="EI",      # acquisition function
            n_calls=1000,       # number of evaluations of f
            n_random_starts=5,  # number of random initialization
            x0=list(x0),
        )

        return settings

    def run_nlopt(self, prior, loglike):
        import nlopt

        def fun(x, grad): # pylint: disable=unused-argument, missing-docstring
            param_vals = np.copy(x)
            #print(param_vals)
            prior(param_vals)
            #print(param_vals)
            llh = loglike(param_vals)
            del param_vals
            return -llh

        # bounds
        lower_bounds = np.zeros(shape=self.n_opt_params)
        upper_bounds = np.ones(shape=self.n_opt_params)

        # for angles make bigger
        for i, name in enumerate(self.hypo_handler.opt_param_names):
            if 'azimuth' in name:
                lower_bounds[i] = -0.5
                upper_bounds[i] = 1.5
            if 'zenith' in name:
                lower_bounds[i] = -0.5
                upper_bounds[i] = 1.5

        # initial guess
        x0 = 0.5 * np.ones(shape=self.n_opt_params)

        # stepsize
        dx = np.zeros(shape=self.n_opt_params)
        for i in range(self.n_opt_params):
            if 'azimuth' in self.hypo_handler.opt_param_names[i]:
                dx[i] = 0.001
            elif 'zenith' in self.hypo_handler.opt_param_names[i]:
                dx[i] = 0.001
            elif self.hypo_handler.opt_param_names[i] in ('x', 'y'):
                dx[i] = 0.005
            elif self.hypo_handler.opt_param_names[i] == 'z':
                dx[i] = 0.002
            elif self.hypo_handler.opt_param_names[i] == 'time':
                dx[i] = 0.01

        # seed from several angles
        #opt = nlopt.opt(nlopt.LN_NELDERMEAD, self.n_opt_params)
        opt = nlopt.opt(nlopt.GN_CRS2_LM, self.n_opt_params)
        #opt = nlopt.opt(nlopt.LN_PRAXIS, self.n_opt_params)
        opt.set_lower_bounds([0.]*self.n_opt_params)
        opt.set_upper_bounds([1.]*self.n_opt_params)
        opt.set_min_objective(fun)
        opt.set_ftol_abs(0.1)
        
        # initial guess

        angles = np.linspace(0,1,3)
        angles = 0.5 * (angles[1:] + angles[:-1])

        for zen in angles:
            for az in angles:
                x0 = 0.5 * np.ones(shape=self.n_opt_params)
                
                for i in range(self.n_opt_params):
                    if 'azimuth' in self.hypo_handler.opt_param_names[i]:
                        x0[i] = az
                    elif 'zenith' in self.hypo_handler.opt_param_names[i]:
                        x0[i] = zen
                x = opt.optimize(x0) # pylint: disable=unused-variable

        #local_opt = nlopt.opt(nlopt.LN_NELDERMEAD, self.n_opt_params)
        #local_opt.set_lower_bounds([0.]*self.n_opt_params)
        #local_opt.set_upper_bounds([1.]*self.n_opt_params)
        #local_opt.set_min_objective(fun)
        ##local_opt.set_ftol_abs(0.5)
        ##local_opt.set_ftol_abs(100)
        ##local_opt.set_xtol_rel(10)
        #local_opt.set_ftol_abs(1)
        # global
        #opt = nlopt.opt(nlopt.G_MLSL, self.n_opt_params)
        #opt.set_lower_bounds([0.]*self.n_opt_params)
        #opt.set_upper_bounds([1.]*self.n_opt_params)
        #opt.set_min_objective(fun)
        #opt.set_local_optimizer(local_opt)
        #opt.set_ftol_abs(10)
        #opt.set_xtol_rel(1)
        #opt.set_maxeval(1111)

        #opt = nlopt.opt(nlopt.GN_ESCH, self.n_opt_params)
        #opt = nlopt.opt(nlopt.GN_ISRES, self.n_opt_params)
        #opt = nlopt.opt(nlopt.GN_CRS2_LM, self.n_opt_params)
        #opt = nlopt.opt(nlopt.GN_DIRECT_L_RAND_NOSCAL, self.n_opt_params)
        #opt = nlopt.opt(nlopt.LN_NELDERMEAD, self.n_opt_params)

        #opt.set_lower_bounds(lower_bounds)
        #opt.set_upper_bounds(upper_bounds)
        #opt.set_min_objective(fun)
        #opt.set_ftol_abs(0.1)
        #opt.set_population([x0])
        #opt.set_initial_step(dx)

        #local_opt.set_maxeval(10)

        #x = opt.optimize(x0) # pylint: disable=unused-variable

        # polish it up
        #print('***************** polishing ******************')

        #dx = np.ones(shape=self.n_opt_params) * 0.001
        #dx[0] = 0.1
        #dx[1] = 0.1

        #local_opt = nlopt.opt(nlopt.LN_NELDERMEAD, self.n_opt_params)
        #lower_bounds = np.clip(np.copy(x) - 0.1, 0, 1)
        #upper_bounds = np.clip(np.copy(x) + 0.1, 0, 1)
        #lower_bounds[0] = 0
        #lower_bounds[1] = 0
        #upper_bounds[0] = 0
        #upper_bounds[1] = 0

        #local_opt.set_lower_bounds(lower_bounds)
        #local_opt.set_upper_bounds(upper_bounds)
        #local_opt.set_min_objective(fun)
        #local_opt.set_ftol_abs(0.1)
        #local_opt.set_initial_step(dx)
        #x = opt.optimize(x)

        settings = OrderedDict()
        settings['method'] = opt.get_algorithm_name()
        settings['ftol_abs'] = opt.get_ftol_abs()
        settings['ftol_rel'] = opt.get_ftol_rel()
        settings['xtol_abs'] = opt.get_xtol_abs()
        settings['xtol_rel'] = opt.get_xtol_rel()
        settings['maxeval'] = opt.get_maxeval()
        settings['maxtime'] = opt.get_maxtime()
        settings['stopval'] = opt.get_stopval()

        return settings

    def run_multinest(
            self,
            prior,
            loglike,
            importance_sampling,
            max_modes,
            const_eff,
            n_live,
            evidence_tol,
            sampling_eff,
            max_iter,
            seed,
    ):
        """Setup and run MultiNest on an event.

        See the README file from MultiNest for greater detail on parameters
        specific to to MultiNest (parameters from `importance_sampling` on).

        Parameters
        ----------
        prior
        loglike
        importance_sampling
        max_modes
        const_eff
        n_live
        evidence_tol
        sampling_eff
        max_iter
            Note that this limit is the maximum number of sample replacements and
            _not_ max number of likelihoods evaluated. A replacement only occurs
            when a likelihood is found that exceeds the minimum likelihood among
            the live points.
        seed

        Returns
        -------
        settings : OrderedDict
            Metadata used for running MultiNest, i.e.
            the keyword args used to invoke the `pymultinest.run` function.

        """
        # Import pymultinest here; it's a less common dependency, so other
        # functions / constants in this module will still be import-able w/o it.
        import pymultinest

        settings = OrderedDict([
            ('n_dims', self.n_opt_params),
            ('n_params', self.n_params),
            ('n_clustering_params', self.n_opt_params),
            ('wrapped_params', [
                ('azimuth' in p.lower()) for p in self.hypo_handler.all_param_names
            ]),
            ('importance_nested_sampling', importance_sampling),
            ('multimodal', max_modes > 1),
            ('const_efficiency_mode', const_eff),
            ('n_live_points', n_live),
            ('evidence_tolerance', evidence_tol),
            ('sampling_efficiency', sampling_eff),
            ('null_log_evidence', -1e90),
            ('max_modes', max_modes),
            ('mode_tolerance', -1e90),
            ('seed', seed),
            ('log_zero', -1e100),
            ('max_iter', max_iter),
        ])

        print('Runing MultiNest...')
        pymultinest.run(
            LogLikelihood=loglike,
            Prior=prior,
            verbose=True,
            outputfiles_basename=self.out_prefix,
            resume=False,
            write_output=False,
            n_iter_before_update=5000,
            **settings
        )

        return settings


def parse_args(description=__doc__):
    """Parse command-line arguments.

    Returns
    -------
    split_kwargs : dict of dicts
        Contains keys "dom_tables_kw", "hypo_kw", "events_kw", and "reco_kw",
        where values are kwargs dicts usable to instantiate or call each of the
        corresponding objects or functions.

    """
    parser = ArgumentParser(description=description)

    parser.add_argument(
        '--outdir', required=True
    )

    group = parser.add_argument_group(
        title='Hypothesis parameter priors',
    )

    group.add_argument(
        '--spatial-prior',
        choices='dc dc_subdust ic SPEFit2'.split(),
        required=True,
        help='''Choose a prior for choosing spatial samples. "dc", "dc_subdust"
        and "ic" are uniform priors with hard cut-offs at the extents of the
        respective volumes, while "SPEFit2" samples from Cauchy distributions
        around the SPEFit2 (x, y, z) best-fit values.'''
    )
    group.add_argument(
        '--temporal-prior',
        choices='uniform SPEFit2'.split(),
        required=True,
        help='''Choose a prior for choosing temporal samples. "uniform" chooses
        uniformly from 4000 ns prior to the first hit up to the last hit, while
        "SPEFit2" samples from a Cauchy distribution near (not *at* due to
        bias) the SPEFit2 time best-fit value.'''
    )
    group.add_argument(
        '--cascade-angle-prior',
        choices=[PRI_UNIFORM, PRI_LOG_NORMAL],
        required=False,
        help='''Prior to put on opening angle between track and cascade.'''
    )
    group.add_argument(
        '--cascade-energy-prior',
        choices=[PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL],
        required=False,
        help='''Prior to put on _total_ event cascade-energy. Must specify
        --cascade-energy-lims.'''
    )
    group.add_argument(
        '--cascade-energy-lims', nargs='+',
        required=False,
        help='''Lower and upper cascade-energy limits, in GeV. E.g.: --cascade-energy-lims=1,100
        Required if --cascade-energy-prior is {}, {}, or {}'''
        .format(PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL)
    )

    group.add_argument(
        '--track-energy-prior',
        choices=[PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL],
        required=False,
        help='''Lower and upper cascade-energy limits, in GeV. E.g.: --cascade-energy-lims=1,100
        Required if --cascade-energy-prior is {}, {}, or {}'''
        .format(PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL)
    )

    group.add_argument(
        '--track-energy-lims', nargs='+',
        required=False,
        help='''Lower and upper track-energy limits, in GeV. E.g.: --track-energy-lims=1,100
        Required if --track-energy-prior is {}, {}, or {}'''
        .format(PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL)
    )

    group = parser.add_argument_group(
        title='MultiNest parameters',
    )

    group.add_argument(
        '--importance-sampling', action='store_true',
        help='''Importance nested sampling (INS) mode. Could be more efficient,
        but also can be unstable. Does not work with multimodal.'''
    )
    group.add_argument(
        '--max-modes', type=int, required=True,
        help='''Set to 1 to disable multi-modal search. Must be 1 if --importance-sampling is
        specified.'''
    )
    group.add_argument(
        '--const-eff', action='store_true',
        help='''Constant efficiency mode.'''
    )
    group.add_argument(
        '--n-live', type=int, required=True
    )
    group.add_argument(
        '--evidence-tol', type=float, required=True
    )
    group.add_argument(
        '--sampling-eff', type=float, required=True
    )
    group.add_argument(
        '--max-iter', type=int, required=True,
        help='''Note that iterations of the MultiNest algorithm are _not_ the
        number of likelihood evaluations. An iteration comes when one live
        point is discarded by finding a sample with higher likelihood than at
        least one other live point. Such a point can take many likelihood
        evaluatsions to find.'''
    )
    group.add_argument(
        '--seed', type=int, required=True,
        help='''Integer seed for MultiNest's random number generator.'''
    )

    split_kwargs = init_obj.parse_args(
        dom_tables=True, hypo=True, events=True, parser=parser
    )

    split_kwargs['reco_kw'] = reco_kw = split_kwargs.pop('other_kw')

    if reco_kw['cascade_energy_prior'] in [PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL]:
        assert reco_kw['cascade_energy_lims'] is not None
        elims = ''.join(reco_kw['cascade_energy_lims'])
        elims = [float(l) for l in elims.split(',')]
        reco_kw['cascade_energy_lims'] = elims
    elif reco_kw['cascade_energy_prior'] is None:
        reco_kw.pop('cascade_energy_prior')
        reco_kw.pop('cascade_energy_lims')
    elif reco_kw['cascade_energy_lims'] is not None:
        raise ValueError('--cascade-energy-lims not used with cascade_energy prior {}'
                         .format(reco_kw['cascade_energy_prior']))

    if reco_kw['track_energy_prior'] in [PRI_UNIFORM, PRI_LOG_UNIFORM, PRI_LOG_NORMAL]:
        assert reco_kw['track_energy_lims'] is not None
        elims = ''.join(reco_kw['track_energy_lims'])
        elims = [float(l) for l in elims.split(',')]
        reco_kw['track_energy_lims'] = elims
    elif reco_kw['track_energy_prior'] is None:
        reco_kw.pop('track_energy_prior')
        reco_kw.pop('track_energy_lims')
    elif reco_kw['track_energy_lims'] is not None:
        raise ValueError('--track-energy-lims not used with track_energy prior {}'
                         .format(reco_kw['track_energy_prior']))

    return split_kwargs


if __name__ == '__main__':
    my_reco = RetroReco(**parse_args()) # pylint: disable=invalid-name
    #my_reco.run(method='multinest')
    #my_reco.run(method='test')
    my_reco.run(method='mymini')
    #my_reco.run(method='truth')
    #my_reco.run(method='nlopt')
