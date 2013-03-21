import json
import re
import os

try:
    # Python 3
    from urllib.parse import urlparse
except (ImportError):
    # Python 2
    from urlparse import urlparse

from ..console_write import console_write
from .release_selector import ReleaseSelector
from ..clients.github_client import GitHubClient
from ..clients.bitbucket_client import BitBucketClient
from ..download_manager import DownloadManager


class PackageProvider(ReleaseSelector):
    """
    Generic repository downloader that fetches package info

    With the current channel/repository architecture where the channel file
    caches info from all includes repositories, these package providers just
    serve the purpose of downloading packages not in the default channel.

    The structure of the JSON a repository should contain is located in
    example-packages.json.

    :param repo:
        The URL of the package repository

    :param settings:
        A dict containing at least the following fields:
          `cache_length`,
          `debug`,
          `timeout`,
          `user_agent`,
          `http_proxy`,
          `https_proxy`,
          `proxy_username`,
          `proxy_password`
    """

    def __init__(self, repo, settings):
        self.repo_info = None
        self.schema_version = 0.0
        self.repo = repo
        self.settings = settings
        self.unavailable_packages = []

    def match_url(self):
        """Indicates if this provider can handle the provided repo"""

        return True

    def fetch_repo(self):
        """Retrieves and loads the JSON for other methods to use"""

        if self.repo_info != None:
            return

        self.repo_info = self.fetch_url(self.repo)
        if self.repo_info == False:
             return False

        if 'includes' not in self.repo_info:
            return

        # Allow repositories to include other repositories
        url_pieces = urlparse(self.repo)
        domain = url_pieces['scheme'] + '://' + url_pieces['netloc']
        path = '/' if url_pieces['path'] == '' else url_pieces['path']
        if path[-1] != '/':
            path = os.path.dirname(path)
        relative_base = domain + path

        includes = self.repo_info.get('includes', [])
        del self.repo_info['includes']
        for include in includes:
            if re.match('^\./|\.\./', include):
                include = os.path.normpath(relative_base + include)
            include_info = self.fetch_url(include)
            if include_info == False:
                continue
            included_packages = include_info.get('packages', [])
            self.repo_info['packages'].extend(included_packages)

    def fetch_url(self, url):
        download_manager = DownloadManager(self.settings)
        json_string = download_manager.download_url(url,
            'Error downloading repository.')
        if json_string == False:
            return json_string

        try:
            return json.loads(json_string.decode('utf-8'))
        except (ValueError):
            console_write(u'Error parsing JSON from repository %s.' % url, True)
            return False

    def get_packages(self):
        """
        Provides access to the repository info that is cached in a channel

        :return:
            A dict in the format:
            {
                'Package Name': {
                    # Package details - see example-packages.json for format
                },
                ...
            }
            or False if there is an error
        """

        self.fetch_repo()
        if self.repo_info == False:
            return False

        output = {}

        schema_error = u'Repository %s does not appear to be a valid repository file because ' % self.repo

        if 'schema_version' not in self.repo_info:
            console_write(u'%s the "schema_version" JSON key is missing.' % schema_error, True)
            return False

        try:
            self.schema_version = float(self.repo_info.get('schema_version'))
        except (ValueError):
            console_write(u'%s the "schema_version" is not a valid number.' % schema_error, True)
            return False

        if self.schema_version not in [1.0, 1.1, 1.2, 2.0]:
            console_write(u'%s the "schema_version" is not recognized. Must be one of: 1.0, 1.1, 1.2 or 2.0.' % schema_error, True)
            return False

        if 'packages' not in self.repo_info:
            console_write(u'%s the "packages" JSON key is missing.' % schema_error, True)
            return False

        github_client = GitHubClient(self.settings)
        bitbucket_client = BitBucketClient(self.settings)

        for package in self.repo_info['packages']:
            info = {}

            for field in ['name', 'description', 'author', 'last_modified']:
                if package.get(field):
                    info[field] = package.get(field)

            if package.get('homepage'):
                info['url'] = package.get('homepage')

            # Schema version 2.0 allows for grabbing details about a pacakge, or its
            # download from "details" urls. See the GitHubClient and BitBucketClient
            # classes for valid URLs.
            if self.schema_version >= 2.0:
                details = package.get('details')
                releases = package.get('releases')
                
                # Try to grab package-level details from GitHub or BitBucket
                if details:
                    github_repo_info = github_client.repo_info(details)
                    bitbucket_repo_info = bitbucket_client.repo_info(details)
                    
                    # When grabbing details, prefer explicit field values over the values
                    # from the GitHub or BitBucket API
                    if github_repo_info:
                        info = dict(github_repo_info.items(), info.items())
                    elif bitbucket_repo_info:
                        info = dict(bitbucket_repo_info.items(), info.items())
                    else:
                        console_write(u'Invalid "details" key for one of the packages in the repository %s.' % self.repo, True)
                        continue

                download_details = None
                download_info = {}

                # If no releases info was specified, also grab the download info from GH or BB
                if not releases and details:
                    releases = [{'details': details}]

                # This allows developers to specify a GH or BB location to get releases from,
                # especially tags URLs (https://github.com/user/repo/tags or
                # https://bitbucket.org/user/repo#tags)
                info['releases'] = []
                for release in releases:
                    # Make sure that explicit fields are copied over
                    for field in ['platforms', 'sublime_text', 'version', 'url', 'date']:
                        if field in releases[0]:
                            download_info[field] = releases[0][field]

                    download_details = releases[0]['details']
                    if download_details:
                        github_download = github_client.download_info(download_details)
                        bitbucket_download = bitbucket_client.download_info(download_details)

                        # Overlay the explicit field values over values fetched from the APIs
                        if github_download:
                            download_info = dict(github_download.items(), download_info.items())
                        elif bitbucket_download:
                            download_info = dict(bitbucket_download.items(), download_info.items())
                        else:
                            console_write(u'Invalid "details" key under the "releases" key for the package "%s" in the repository %s.' % (info['name'], self.repo), True)
                            continue

                    info['releases'].append(download_info)
            
                info = self.select_release(info)

            # Schema version 1.0, 1.1 and 1.2 just require that all values be
            # explicitly specified in the package JSON
            else:
                info['platforms'] = package.get('platforms')
                info = self.select_platform(info)

            if not info:
                self.unavailable_packages.append(package['name'])
                continue

            if 'download' not in info:
                console_write(u'No "releases" key for the package "%s" in the repository %s.' % (info['name'], self.repo), True)
                continue

            if 'url' not in info:
                info['url'] = self.repo

            # Rewrites the legacy "zipball" URLs to the new "zip" format
            info['download']['url'] = re.sub(
                '^(https://nodeload.github.com/[^/]+/[^/]+/)zipball(/.*)$',
                '\\1zip\\2', info['download']['url'])

            output[info['name']] = info

        return output

    def get_renamed_packages(self):
        """:return: A dict of the packages that have been renamed"""

        if self.schema_version < 2.0:
            return self.repo_info.get('renamed_packages', {})

        output = {}
        for package in self.repo_info['packages']:
            if 'previous_names' not in package:
                continue

            previous_names = package['previous_names']
            if not isinstance(previous_names, list):
                previous_names = [previous_names]

            for previous_name in previous_names:
                output[previous_name] = package['name']

        return output

    def get_unavailable_packages(self):
        """
        Provides a list of packages that are unavailable for the current
        platform/architecture that Sublime Text is running on.

        This list will be empty unless get_packages() is called first.

        :return: A list of package names
        """

        return self.unavailable_packages