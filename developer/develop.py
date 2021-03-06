from __future__ import print_function, division, absolute_import

import logging
import numpy as np
import pandas as pd

import developer.utils as utils
from developer import proposal_select
from numbers import Number

logger = logging.getLogger(__name__)


class Developer(object):
    """
    Pass the dataframe that is returned by feasibility here

    Can also be a dictionary where keys are building forms and values are
    the individual data frames returned by the proforma lookup routine.

    Parameters
    ----------
    feasibility : DataFrame or dict
        Results from SqftProForma lookup method
    forms : string or list
        One or more of the building forms from the pro forma specification -
        e.g. "residential" or "mixedresidential" - these are configuration
        parameters passed previously to the pro forma.  If more than one form
        is passed the forms compete with each other (based on profitability)
        for which one gets built in order to meet demand.
    parcel_size : series
        The size of the parcels.  This was passed to feasibility as well,
        but should be passed here as well.  Index should be parcel_ids.
    ave_unit_size : series
        The average residential unit size around each parcel - this is
        indexed by parcel, but is usually a disaggregated version of a
        zonal or accessibility aggregation.
    current_units : series
        The current number of units on the parcel.  Is used to compute the
        net number of units produced by the developer model.  Many times
        the developer model is redeveloping units (demolishing them) and
        is trying to meet a total number of net units produced.
    year : int
        The year of the simulation - will be assigned to 'year_built' on the
        new buildings
    bldg_sqft_per_job : float (default 400.0)
        The average square feet per job for this building form.
    min_unit_size : float
        Values less than this number in ave_unit_size will be set to this
        number.  Deals with cases where units are currently not built.
    max_parcel_size : float
        Parcels larger than this size will not be considered for
        development - usually large parcels should be specified manually
        in a development projects table.
    drop_after_build : bool
        Whether or not to drop parcels from consideration after they
        have been chosen for development.  Usually this is true so as
        to not develop the same parcel twice.
    residential: bool
        If creating non-residential buildings set this to false and
        developer will fill in job_spaces rather than residential_units
    num_units_to_build: optional, int
        If num_units_to_build is passed, build this many units rather than
        computing it internally by using the length of agents adn the sum of
        the relevant supply columin - this trusts the caller to know how to
        compute this.
    keep_suboptimal: optional, int
        Whether or not to retain all proposals in the feasibility table
        instead of dropping sub-optimal forms and proposals. If setting this
        to True, feasibility table must be in "lonng" form rather than "wide"
        form, with one row per proposal and each proposal pertaining to a
        single form.  At the proposal selection step, this allows
        consideration of feasible proposals for a given parcel that may not be
        the optimal form and which may not be the optimal proposal within a
        given form.

    """

    def __init__(self, feasibility, forms, target_units,
                 parcel_size, ave_unit_size, current_units,
                 year=None, bldg_sqft_per_job=400.0,
                 min_unit_size=400, max_parcel_size=200000,
                 drop_after_build=True, residential=True,
                 num_units_to_build=None, keep_suboptimal=False):

        if isinstance(feasibility, dict):
            feasibility = pd.concat(feasibility.values(),
                                    keys=feasibility.keys(), axis=1)
        self.feasibility = feasibility
        self.forms = forms
        self.target_units = target_units
        self.parcel_size = parcel_size
        self.ave_unit_size = ave_unit_size
        self.current_units = current_units
        self.year = year
        self.bldg_sqft_per_job = bldg_sqft_per_job
        self.min_unit_size = min_unit_size
        self.max_parcel_size = max_parcel_size
        self.drop_after_build = drop_after_build
        self.residential = residential
        self.num_units_to_build = num_units_to_build
        self.keep_suboptimal = keep_suboptimal

    @classmethod
    def from_yaml(cls, feasibility, forms, target_units,
                  parcel_size, ave_unit_size, current_units,
                  year=None, yaml_str=None, str_or_buffer=None,
                  keep_suboptimal=False):
        """
        Parameters
        ----------
        yaml_str : str, optional
            A YAML string from which to load model.
        str_or_buffer : str or file like, optional
            File name or buffer from which to load YAML.

        Returns
        -------
        Developer object
        """
        cfg = utils.yaml_to_dict(yaml_str, str_or_buffer)
        keep_suboptimal = cfg.get('keep_suboptimal', keep_suboptimal)

        model = cls(
            feasibility, forms, target_units,
            parcel_size, ave_unit_size, current_units,
            year, cfg['bldg_sqft_per_job'],
            cfg['min_unit_size'], cfg['max_parcel_size'],
            cfg['drop_after_build'], cfg['residential'],
            keep_suboptimal=keep_suboptimal
        )

        logger.debug('loaded Developer model from YAML')
        return model

    @property
    def to_dict(self):
        """
        Return a dict representation of a Developer instance.

        """
        attributes = ['bldg_sqft_per_job',
                      'min_unit_size', 'max_parcel_size',
                      'drop_after_build', 'residential', 'keep_suboptimal']

        results = {}
        for attribute in attributes:
            results[attribute] = self.__dict__[attribute]

        return results

    def to_yaml(self, str_or_buffer=None):
        """
        Save a model representation to YAML.

        Parameters
        ----------
        str_or_buffer : str or file like, optional
            By default a YAML string is returned. If a string is
            given here the YAML will be written to that file.
            If an object with a ``.write`` method is given the
            YAML will be written to that object.

        Returns
        -------
        j : str
            YAML is string if `str_or_buffer` is not given.

        """
        logger.debug('serializing Developer model to YAML')
        return utils.convert_to_yaml(self.to_dict, str_or_buffer)

    def pick(self, profit_to_prob_func=None, custom_selection_func=None):
        """
        Choose the buildings from the list that are feasible to build in
        order to match the specified demand.

        Parameters
        ----------
        profit_to_prob_func: function
            As there are so many ways to turn the development feasibility
            into a probability to select it for building, the user may pass
            a function which takes the feasibility dataframe and returns
            a series of probabilities.  If no function is passed, the behavior
            of this method will not change
        custom_selection_func: func
            User passed function that decides how to select buildings for
            development after probabilities are calculated. Must have
            parameters (self, df, p) and return a numpy array of buildings to
            build (i.e. df.index.values)

        Returns
        -------
        None if there are no feasible buildings
        new_buildings : dataframe
            DataFrame of buildings to add.  These buildings are rows from the
            DataFrame that is returned from feasibility.
        """
        empty_warn = "WARNING THERE ARE NO FEASIBLE BUILDINGS TO CHOOSE FROM"

        if len(self.feasibility) == 0 or self.feasibility.empty:
            print(empty_warn)
            return

        # Get DataFrame of potential buildings from SqFtProForma steps
        # Unnecessary if feasibility table is already in long-form, as is the
        # case if running developer with sub-optimal proposals retained.
        if not self.keep_suboptimal:
            df = self._get_dataframe_of_buildings()
        else:
            df = self.feasibility

        df = self._remove_infeasible_buildings(df)
        df = self._calculate_net_units(df)

        if len(df) == 0 or df.empty:
            print(empty_warn)
            return

        print("Sum of net units that are profitable: {:,}".format(
            int(df.net_units.sum())))

        # Parcel id needs to be a column rather than the index if
        # selecting proposals with multiple proposals per parcel
        if self.keep_suboptimal:
            df.index.name = 'parcel_id'
            df = df.reset_index()

        # Generate development probabilities and pick buildings to build
        p, df = self._calculate_probabilities(df, profit_to_prob_func)

        # Select proposals to build
        build_idx = self._select_buildings(df, p, custom_selection_func)

        # Drop built buildings from self.feasibility attribute if desired
        if not self.keep_suboptimal:
            self._drop_built_buildings(build_idx)

        # Prep DataFrame of new buildings
        new_df = self._prepare_new_buildings(df, build_idx)

        return new_df

    def _get_dataframe_of_buildings(self):
        """
        Helper method to pick(). Returns a DataFrame of buildings from
        self.feasibility based on what type is passed to self.forms

        Returns
        -------
        df : DataFrame
        """
        if self.forms is None or isinstance(self.forms, list):
            df = self.keep_form_with_max_profit(self.forms)
        else:
            df = self.feasibility[self.forms]

        return df

    @staticmethod
    def _max_form(f, colname):
        """
        Assumes dataframe with hierarchical columns with first index equal to
        the use and second index equal to the attribute.

        e.g. f.columns equal to::

            mixedoffice   building_cost
                          building_revenue
                          building_size
                          max_profit
                          max_profit_far
                          total_cost
            industrial    building_cost
                          building_revenue
                          building_size
                          max_profit
                          max_profit_far
                          total_cost
        """
        df = f.stack(level=0)[[colname]].stack().unstack(level=1).reset_index(
            level=1, drop=True)
        return df.idxmax(axis=1)

    def keep_form_with_max_profit(self, forms=None):
        """
        This converts the dataframe, which shows all profitable forms,
        to the form with the greatest profit, so that more profitable
        forms outcompete less profitable forms.

        Parameters
        ----------
        forms : list of strings
            List of forms to evaluate. If empty or None, all forms are
            evaluated.

        Returns
        -------
        DataFrame consisting of a subset of self.feasibility, where only
        the most profitable form for each parcel is included.

        """
        f = self.feasibility

        if forms is not None:
            f = f[forms]

        if len(f) > 0:
            mu = self._max_form(f, "max_profit")
            indexes = [tuple(x) for x in mu.reset_index().values]
        else:
            indexes = []
        df = f.stack(level=0).loc[indexes]
        df.index.names = ["parcel_id", "form"]
        df = df.reset_index(level=1)
        return df

    def _remove_infeasible_buildings(self, df):
        """
        Helper method to pick(). Removes buildings from the DataFrame if:
            - max_profit_far is 0 or less
            - parcel_size is larger than max_parcel_size

        Also calculates useful DataFrame columns from object attributes
        for later calculations.

        Parameters
        ----------
        df : DataFrame
            DataFrame of buildings from _get_dataframe_of_buildings()

        Returns
        -------
        df : DataFrame
        """
        if len(df) == 0 or df.empty:
            return df

        df = df[df.max_profit_far > 0]
        self.ave_unit_size[
            self.ave_unit_size < self.min_unit_size
        ] = self.min_unit_size
        df.loc[:, 'ave_unit_size'] = self.ave_unit_size
        df.loc[:, 'parcel_size'] = self.parcel_size
        df.loc[:, 'current_units'] = self.current_units
        df = df[df.parcel_size < self.max_parcel_size]

        df['residential_units'] = (df.residential_sqft /
                                   df.ave_unit_size).round()
        df['job_spaces'] = (df.non_residential_sqft /
                            self.bldg_sqft_per_job).round()

        return df

    def _calculate_net_units(self, df):
        """
        Helper method to pick(). Calculates the net_units column,
        and removes buildings that have net_units of 0 or less.

        Parameters
        ----------
        df : DataFrame
            DataFrame of buildings from _remove_infeasible_buildings()

        Returns
        -------
        df : DataFrame
        """
        if len(df) == 0 or df.empty:
            return df

        if self.residential:
            df['net_units'] = df.residential_units - df.current_units
        else:
            df['net_units'] = df.job_spaces - df.current_units
        return df[df.net_units > 0]

    @staticmethod
    def _calculate_probabilities(df, profit_to_prob_func):
        """
        Helper method to pick(). Calculates development probabilities based on
        a preset rule, or an optional function passed to the constructor.

        Parameters
        ----------
        df : DataFrame
            DataFrame of buildings, prepared via _get_dataframe_of_buildings,
            _remove_infeasible_buildings, and _calculate_net_units methods
        profit_to_prob_func : function, optional
            Function to calculate development probabilities for each building

        Returns
        -------
        p : Series
            Series of development probability for each building
        df : DataFrame
            DataFrame of buildings
        """

        if profit_to_prob_func:
            p = profit_to_prob_func(df)
        else:
            df['max_profit_per_size'] = df.max_profit / df.parcel_size
            p = df.max_profit_per_size / df.max_profit_per_size.sum()
        return p, df

    def _select_buildings(self, df, p, custom_selection_func):
        """
        Helper method to pick(). Selects buildings to build based on
        development probabilities.

        Parameters
        ----------
        df : DataFrame
            DataFrame of buildings from _calculate_probabilities method
        p : Series
            Probabilities from _calculate_probabilities method
        custom_selection_func: func
            User passed function that decides how to select buildings for
            development after probabilities are calculated. Must have
            parameters (self, df, p) and return a numpy array of buildings to
            build (i.e. df.index.values)

        Returns
        -------
        build_idx : ndarray
            Index of buildings selected for development

        """
        warning = "WARNING THERE ARE NOT ENOUGH PROFITABLE UNITS TO " \
                  "MATCH DEMAND"
        if isinstance(self.target_units, Number):
            insufficient_units = df.net_units.sum() < self.target_units
            if insufficient_units:
                print(warning)
        elif isinstance(self.target_units, pd.DataFrame):
            insufficient_units = \
                df.net_units.sum() < self.target_units.target_units.sum()
            if insufficient_units:
                print(warning)

        if custom_selection_func is not None:
            build_idx = custom_selection_func(self, df, p, self.target_units)

        elif self.target_units <= 0:
            build_idx = []

        elif self.keep_suboptimal:
            build_idx = proposal_select.weighted_random_choice_multiparcel(df,
                                                          p, self.target_units)  # noqa

        else:
            if insufficient_units:
                build_idx = df.index.values
            else:
                build_idx = proposal_select.weighted_random_choice(df, p,
                                                             self.target_units)  # noqa

        return build_idx

    def _drop_built_buildings(self, build_idx):
        """
        Helper method to pick(). Drops built buildings from the
        self.feasibility attribute DataFrame.

        Parameters
        ----------
        build_idx : Array-like
            Index of buildings selected for development, from
            _buildings_to_build method

        Returns
        -------
        None
        """

        if self.drop_after_build:
            self.feasibility = self.feasibility.drop(build_idx)

    def _prepare_new_buildings(self, df, build_idx):
        """
        Helper method to pick(). Brings parcel_id into a column and applies
        other compatibility fixes before returning to parcel model

        Parameters
        ----------
        df : DataFrame
            DataFrame of buildings from the _calculate_probabilities method
        build_idx : Array-like
            Index of buildings selected for development, from
            _buildings_to_build method

        Returns
        -------
        new_df : DataFrame

        """

        new_df = df.loc[build_idx]

        drop = True
        if 'parcel_id' not in df.columns:
            new_df.index.name = "parcel_id"
            drop = False

        if self.year is not None:
            new_df["year_built"] = self.year

        if not isinstance(self.forms, list):
            # form gets set only if forms is a list
            new_df["form"] = self.forms

        new_df["stories"] = new_df.stories.apply(np.ceil)

        return new_df.reset_index(drop=drop)
