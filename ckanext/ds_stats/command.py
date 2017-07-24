import os
import re
import logging
import datetime
import time

from pylons import config
from ckan.lib.cli import CkanCommand
import ckan.model as model

import dbutil
import ds_stats_model

log = logging.getLogger('ckanext.ds_stats')
PACKAGE_URL = '/dataset/'  # XXX get from routes...
DEFAULT_RESOURCE_URL_TAG = '/downloads/'

RESOURCE_URL_REGEX = re.compile('/dataset/[a-z0-9-_]+/resource/([a-z0-9-_]+)')
DATASET_EDIT_REGEX = re.compile('/dataset/edit/([a-z0-9-_]+)')


class InitDBGaReport(CkanCommand):
    """Initialise the extension's database tables
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 0
    min_args = 0

    def command(self):
        self._load_config()

        import ckan.model as model
        model.Session.remove()
        model.Session.configure(bind=model.meta.engine)
        log = logging.getLogger('ckanext.ds_stats.ga_report')

        import ga_model
        ga_model.init_tables()
        log.info("DB tables are setup")


class FixTimePeriods(CkanCommand):
    """
    Fixes the 'All' records for GA_Urls

    It is possible that older urls that haven't recently been visited
    do not have All records.  This command will traverse through those
    records and generate valid All records for them.
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 0
    min_args = 0

    def __init__(self, name):
        super(FixTimePeriods, self).__init__(name)

    def command(self):
        import ckan.model as model
        from ga_model import post_update_url_stats
        self._load_config()
        model.Session.remove()
        model.Session.configure(bind=model.meta.engine)

        log = logging.getLogger('ckanext.ga_report')

        log.info("Updating 'All' records for old URLs")
        post_update_url_stats()
        log.info("Processing complete")


class LoadAnalyticsGaReport(CkanCommand):
    """Get data from Google Analytics API and save it
    in the ga_model

    Usage: paster loadanalytics <time-period>

    Where <time-period> is:
        all         - data for all time
        latest      - (default) just the 'latest' data
        YYYY-MM     - just data for the specific month
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 1
    min_args = 0

    def __init__(self, name):
        super(LoadAnalyticsGaReport, self).__init__(name)
        self.parser.add_option('-d', '--delete-first',
                               action='store_true',
                               default=False,
                               dest='delete_first',
                               help='Delete data for the period first')
        self.parser.add_option(
            '-s', '--skip_url_stats',
            action='store_true',
            default=False,
            dest='skip_url_stats',
            help='Skip the download of URL data - just do site-wide stats')
        self.token = ""

    def command(self):
        self._load_config()

        from download_analytics import DownloadAnalytics
        from ga_auth import (init_service, get_profile_id)

        ga_token_filepath = os.path.expanduser(config.get(
            'ds_stats.ga.token.filepath', ''))
        if not ga_token_filepath:
            print 'ERROR: In the CKAN config you need to specify the ' \
                  'filepath of the  Google Analytics token file under ' \
                  'key: ds_stats.ga.token.filepath'
            return

        try:
            self.token, svc = init_service(ga_token_filepath)
        except TypeError:
            print ('Have you correctly run the getauthtoken task and '
                   'specified the correct token file in the CKAN config under '
                   '"ds_stats.ga.token.filepath"?')
            return

        downloader = DownloadAnalytics(
            svc, self.token, profile_id=get_profile_id(svc),
            delete_first=self.options.delete_first,
            skip_url_stats=self.options.skip_url_stats)

        time_period = self.args[0] if self.args else 'latest'
        if time_period == 'all':
            downloader.all_()
        elif time_period == 'latest':
            downloader.latest()
        else:
            # The month to use
            for_date = datetime.datetime.strptime(time_period, '%Y-%m')
            downloader.specific_month(for_date)


class InitDBGA(CkanCommand):
    """Initialise the local stats database tables
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 0
    min_args = 0

    def command(self):
        self._load_config()
        model.Session.remove()
        model.Session.configure(bind=model.meta.engine)
        log = logging.getLogger('ckanext.ds_stats.ga')
        dbutil.init_tables()
        log.info("Set up statistics tables in main database")


class LoadAnalyticsGA(CkanCommand):
    """Parse data from Google Analytics API and store it
    in a local database

    Options:
        <credentials_file> internal [date] use ckan internal tracking tables
                        credentials_file specifies the OAUTH credentials file
                        date specifies start date for retrieving
                        analytics data YYYY-MM-DD format
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 3
    min_args = 1
    TEST_HOST = None
    CONFIG = None

    def command(self):
        if not self.CONFIG:
            self._load_config()
            self.CONFIG = config

        self.resource_url_tag = self.CONFIG.get(
            'ds_stats.ga.resource_prefix',
            DEFAULT_RESOURCE_URL_TAG)

        # funny dance we need to do to make sure we've got a
        # configured session
        model.Session.remove()
        model.Session.configure(bind=model.meta.engine)
        self.parse_and_save()

    def internal_save(self, packages_data, summary_date):
        engine = model.meta.engine
        # clear out existing data before adding new
        sql = '''DELETE FROM tracking_summary
                 WHERE tracking_date='%s'; ''' % summary_date
        engine.execute(sql)

        for url, count in packages_data.iteritems():
            # If it matches the resource then we should mark it as a resource.
            # For resources we don't currently find the package ID.
            if RESOURCE_URL_REGEX.match(url):
                tracking_type = 'resource'
            else:
                tracking_type = 'page'

            sql = '''INSERT INTO tracking_summary
                     (url, count, tracking_date, tracking_type)
                     VALUES (%s, %s, %s, %s);'''
            engine.execute(sql, url, count, summary_date, tracking_type)

        # get ids for dataset urls
        sql = '''UPDATE tracking_summary t
                 SET package_id = COALESCE(
                     (SELECT id FROM package p WHERE t.url =  %s || p.name)
                     ,'~~not~found~~')
                 WHERE t.package_id IS NULL AND tracking_type = 'page';'''
        engine.execute(sql, PACKAGE_URL)

        # get ids for dataset edit urls which aren't captured otherwise
        sql = '''UPDATE tracking_summary t
                 SET package_id = COALESCE(
                     (SELECT id FROM package p WHERE t.url =  %s || p.name)
                     ,'~~not~found~~')
                 WHERE t.package_id='~~not~found~~' AND tracking_type='page';
              '''
        engine.execute(sql, '%sedit/' % PACKAGE_URL)

        # update summary totals for resources
        sql = '''UPDATE tracking_summary t1
                 SET running_total = (
                    SELECT sum(count)
                    FROM tracking_summary t2
                    WHERE t1.url = t2.url
                    AND t2.tracking_date <= t1.tracking_date
                 ) + t1.count
                 ,recent_views = (
                    SELECT sum(count)
                    FROM tracking_summary t2
                    WHERE t1.url = t2.url
                    AND t2.tracking_date <= t1.tracking_date
                    AND t2.tracking_date >= t1.tracking_date - 14
                 ) + t1.count
                 WHERE t1.running_total = 0 AND tracking_type = 'resource';'''
        engine.execute(sql)

        # update summary totals for pages
        sql = '''UPDATE tracking_summary t1
                 SET running_total = (
                    SELECT sum(count)
                    FROM tracking_summary t2
                    WHERE t1.package_id = t2.package_id
                    AND t2.tracking_date <= t1.tracking_date
                 ) + t1.count
                 ,recent_views = (
                    SELECT sum(count)
                    FROM tracking_summary t2
                    WHERE t1.package_id = t2.package_id
                    AND t2.tracking_date <= t1.tracking_date
                    AND t2.tracking_date >= t1.tracking_date - 14
                 ) + t1.count
                 WHERE t1.running_total = 0 AND tracking_type = 'page'
                 AND t1.package_id IS NOT NULL
                 AND t1.package_id != '~~not~found~~';'''
        engine.execute(sql)

    def bulk_import(self):
        if len(self.args) == 3:
            # Get summeries from specified date
            start_date = datetime.datetime.strptime(self.args[2], '%Y-%m-%d')
        else:
            # No date given. See when we last have data for and get data
            # from 2 days before then in case new data is available.
            # If no date here then use 2010-01-01 as the start date
            engine = model.meta.engine
            sql = '''SELECT tracking_date from tracking_summary
                     ORDER BY tracking_date DESC LIMIT 1;'''
            result = engine.execute(sql).fetchall()
            if result:
                start_date = result[0]['tracking_date']
                start_date += datetime.timedelta(-2)
                # convert date to datetime
                combine = datetime.datetime.combine
                start_date = combine(start_date, datetime.time(0))
            else:
                start_date = datetime.datetime(2011, 1, 1)
        end_date = datetime.datetime.now()
        while start_date < end_date:
            stop_date = start_date + datetime.timedelta(1)
            packages_data = self.get_ga_data_new(start_date=start_date,
                                                 end_date=stop_date)
            self.internal_save(packages_data, start_date)
            # sleep to rate limit requests
            time.sleep(0.25)
            start_date = stop_date
            log.info('%s received %s' % (len(packages_data), start_date))
            print '%s received %s' % (len(packages_data), start_date)

    def get_ga_data_new(self, start_date=None, end_date=None):
        """Get raw data from Google Analtyics for packages and
        resources.

        Returns a dictionary like::

           {'identifier': 3}
        """
        start_date = start_date.strftime("%Y-%m-%d")
        end_date = end_date.strftime("%Y-%m-%d")

        packages = {}
        query = 'ga:pagePath=~%s,ga:pagePath=~%s' % \
            (PACKAGE_URL, self.resource_url_tag)
        metrics = 'ga:uniquePageviews'
        sort = '-ga:uniquePageviews'

        start_index = 1
        max_results = 10000
        # data retrival is chunked
        completed = False
        while not completed:
            results = self.service.data().ga().get(
                ids='ga:%s' % self.profile_id,
                filters=query,
                dimensions='ga:pagePath',
                start_date=start_date,
                start_index=start_index,
                max_results=max_results,
                metrics=metrics,
                sort=sort,
                end_date=end_date).execute()
            result_count = len(results.get('rows', []))
            if result_count < max_results:
                completed = True

            for result in results.get('rows', []):
                package = result[0]
                package = '/' + '/'.join(package.split('/')[2:])
                count = result[1]
                packages[package] = int(count)

            start_index += max_results

            # rate limiting
            time.sleep(0.2)
        return packages

    def parse_and_save(self):
        """Grab raw data from Google Analytics and save to the database"""
        from ga_auth import (init_service, get_profile_id)

        tokenfile = self.args[0]
        if not os.path.exists(tokenfile):
            raise Exception('Cannot find the token file %s' % self.args[0])

        try:
            self.service = init_service(self.args[0])
            from googleapiclient.discovery import Resource
            if not isinstance(self.service, Resource):
                self.service = self.service[1]
        except TypeError as e:
            raise Exception('Unable to create a service: {0}'.format(e))
        self.profile_id = get_profile_id(self.service)

        if len(self.args) > 1:
            if len(self.args) > 2 and self.args[1].lower() != 'internal':
                raise Exception('Illegal argument %s' % self.args[1])
            self.bulk_import()
        else:
            query = 'ga:pagePath=~%s,ga:pagePath=~%s' % \
                    (PACKAGE_URL, self.resource_url_tag)
            packages_data = self.get_ga_data(query_filter=query)
            self.save_ga_data(packages_data)
            log.info("Saved %s records from google" % len(packages_data))

    def save_ga_data(self, packages_data):
        """Save tuples of packages_data to the database
        """
        for identifier, visits in packages_data.items():
            recently = visits.get('recent', 0)
            ever = visits.get('ever', 0)
            matches = RESOURCE_URL_REGEX.match(identifier)
            if matches:
                resource_url = identifier[len(self.resource_url_tag):]
                resource = model.Session.query(model.Resource).autoflush(True)\
                    .filter_by(id=matches.group(1)).first()
                if not resource:
                    log.warning("Couldn't find resource %s" % resource_url)
                    continue
                dbutil.update_resource_visits(resource.id, recently, ever)
                log.info("Updated %s with %s visits" % (resource.id, visits))
            else:
                package_name = identifier[len(PACKAGE_URL):]
                if "/" in package_name:
                    log.warning("%s not a valid package name" % package_name)
                    continue
                item = model.Package.by_name(package_name)
                if not item:
                    log.warning("Couldn't find package %s" % package_name)
                    continue
                dbutil.update_package_visits(item.id, recently, ever)
                log.info("Updated %s with %s visits" % (item.id, visits))
        model.Session.commit()

    def ga_query(self, query_filter=None, from_date=None, to_date=None,
                 start_index=1, max_results=10000, metrics=None, sort=None):
        """Execute a query against Google Analytics
        """
        if not to_date:
            now = datetime.datetime.now()
            to_date = now.strftime("%Y-%m-%d")
        if isinstance(from_date, datetime.date):
            from_date = from_date.strftime("%Y-%m-%d")
        if not metrics:
            metrics = 'ga:visits,ga:visitors,ga:newVisits,ga:uniquePageviews'
        if not sort:
            sort = '-ga:uniquePageviews'

        print '%s -> %s' % (from_date, to_date)

        results = self.service.data().ga().get(
            ids='ga:' + self.profile_id,
            start_date=from_date,
            end_date=to_date,
            dimensions='ga:pagePath',
            metrics=metrics,
            sort=sort,
            start_index=start_index,
            filters=query_filter,
            max_results=max_results
            ).execute()
        return results

    def get_ga_data(self, query_filter=None, start_date=None, end_date=None):
        """Get raw data from Google Analtyics for packages and
        resources, and for both the last two weeks and ever.

        Returns a dictionary like::

           {'identifier': {'recent':3, 'ever':6}}
        """
        now = datetime.datetime.now()
        recent_date = now - datetime.timedelta(14)
        recent_date = recent_date.strftime("%Y-%m-%d")
        floor_date = datetime.date(2005, 1, 1)
        packages = {}
        queries = ['ga:pagePath=~%s' % PACKAGE_URL]
        dates = {'recent': recent_date, 'ever': floor_date}
        for date_name, date in dates.iteritems():
            for query in queries:
                results = self.ga_query(query_filter=query,
                                        metrics='ga:uniquePageviews',
                                        from_date=date)
                if 'rows' in results:
                    for result in results.get('rows'):
                        package = result[0]
                        if not package.startswith(PACKAGE_URL):
                            package = '/' + '/'.join(package.split('/')[2:])

                        count = result[1]
                        # Make sure we add the different representations of the
                        # same dataset /mysite.com & /www.mysite.com ...
                        val = 0
                        if (package in packages
                                and date_name in packages[package]):
                            val += packages[package][date_name]
                        packages.setdefault(package, {})[date_name] = \
                            int(count) + val
        return packages


class InitDBDsStatsCache(CkanCommand):
    """Initialise the local stats database tables
    """
    summary = __doc__.split('\n')[0]
    usage = __doc__
    max_args = 0
    min_args = 0

    def command(self):
        self._load_config()
        model.Session.remove()
        model.Session.configure(bind=model.meta.engine)
        log = logging.getLogger('ckanext.ds_stats.stats')
        ds_stats_model.create_table()
        ds_stats_model.update_table()
        log.info("Set up ds_stats_cache table in main database")
        log.info("Set up default values for ds_stats_cache table")
