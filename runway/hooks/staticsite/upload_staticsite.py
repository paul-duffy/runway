"""CFNgin hook for syncing static website to S3 bucket."""
# TODO move to runway.cfngin.hooks on next major release
import logging
import time
import os
import json
import hashlib
from operator import itemgetter

import yaml

from ...cfngin.lookups.handlers.output import OutputLookup
from ...commands.runway.run_aws import aws_cli

LOGGER = logging.getLogger(__name__)


def get_archives_to_prune(archives, hook_data):
    """Return list of keys to delete.

    Keyword Args:
        archives (Dict): The full list of file archives
        hook_data (Dict): CFNgin hook data

    """
    files_to_skip = []

    for i in ['current_archive_filename', 'old_archive_filename']:
        if hook_data.get(i):
            files_to_skip.append(hook_data[i])

    archives.sort(key=itemgetter('LastModified'), reverse=False)  # sort from oldest to newest

    # Drop all but last 15 files
    return [i['Key'] for i in archives[:-15] if i['Key'] not in files_to_skip]


def sync(context, provider, **kwargs):
    """Sync static website to S3 bucket.

    Keyword Args:

        context (:class:`runway.cfngin.context.Context`): The context
            instance.
        provider (:class:`runway.cfngin.providers.base.BaseProvider`):
            The provider instance.

    """
    session = context.get_session()
    bucket_name = OutputLookup.handle(kwargs.get('bucket_output_lookup'),
                                      provider=provider,
                                      context=context)
    build_context = context.hook_data['staticsite']
    invalidate_cache = False

    extra_files = sync_extra_files(
        context,
        bucket_name,
        kwargs.get('extra_files', []),
        hash_tracking_parameter=build_context.get('hash_tracking_parameter')
    )

    if len(extra_files) > 0:
        invalidate_cache = True

    if build_context['deploy_is_current']:
        LOGGER.info('staticsite: skipping upload; latest version already deployed')
    else:
        # Using the awscli for s3 syncing is incredibly suboptimal, but on
        # balance it's probably the most stable/efficient option for syncing
        # the files until https://github.com/boto/boto3/issues/358 is resolved
        sync_args = ['s3',
                     'sync',
                     build_context['app_directory'],
                     "s3://%s/" % bucket_name,
                     '--delete']

        for extra_file in [f['name'] for f in kwargs.get('extra_files', [])]:
            sync_args.extend(['--exclude', extra_file])

        aws_cli(sync_args)

        invalidate_cache = True

        LOGGER.info("staticsite: sync " "complete")

        update_ssm_hash(context, session)

    if kwargs.get('cf_disabled', False):
        display_static_website_url(kwargs.get('website_url'), provider, context)

    if invalidate_cache:
        distribution = get_distribution_data(context, provider, **kwargs)
        invalidate_distribution(session, **distribution)

    prune_archives(context, session)

    return True


def display_static_website_url(website_url_handle, provider, context):
    """Based on the url handle display the static website url.

    Keyword Args:
        website_url_handle (str): the Output handle for the website url
        provider (:class:`runway.cfngin.providers.base.BaseProvider`):
            The provider instance.
        context (:class:`runway.cfngin.context.Context`): context instance

    """
    bucket_url = OutputLookup.handle(website_url_handle,
                                     provider=provider,
                                     context=context)
    LOGGER.info("STATIC WEBSITE URL: %s", bucket_url)


def update_ssm_hash(context, session):
    """Update the SSM hash with the new tracking data.

    Keyword Args:
        context (:class:`runway.cfngin.context.Context`): context instance
        session (:class:`runway.cfngin.session.Session`): CFNgin session

    """
    build_context = context.hook_data['staticsite']

    if not build_context.get('hash_tracking_disabled'):
        hash_param = build_context['hash_tracking_parameter']
        hash_value = build_context['hash']

        LOGGER.info("staticsite: updating environment SSM parameter %s with hash %s",
                    hash_param,
                    hash_value)

        set_ssm_value(session, hash_param, hash_value,
                      'Hash of currently deployed static website source')

    return True


def get_distribution_data(context, provider, **kwargs):
    """Retrieve information about the distribution.

    Keyword Args:
        context (:class:`runway.cfngin.context.Context`): The context
            instance.
        provider (:class:`runway.cfngin.providers.base.BaseProvider`):
            The provider instance

    """
    LOGGER.info("Retrieved distribution data")
    return {
        'identifier': OutputLookup.handle(
            kwargs.get('distributionid_output_lookup'),
            provider=provider,
            context=context
        ),
        'domain': OutputLookup.handle(
            kwargs.get('distributiondomain_output_lookup'),
            provider=provider,
            context=context
        ),
        'path': kwargs.get('distribution_path', '/*')
    }


def invalidate_distribution(session, identifier='', path='', domain='', **_):
    """Invalidate the current distribution.

    Keyword Args:
        session (Session): The current CFNgin session
        identifier (string): The distribution id
        path (string): The distribution path
        domain (string): The distribution domain

    """
    LOGGER.info("staticsite: Invalidating CF distribution")
    cf_client = session.client('cloudfront')
    cf_client.create_invalidation(
        DistributionId=identifier,
        InvalidationBatch={
            'Paths': {
                'Quantity': 1,
                'Items': [path]},
            'CallerReference': str(time.time())}
    )

    LOGGER.info("staticsite: CF invalidation of %s (domain %s) " "complete", identifier, domain)
    return True


def prune_archives(context, session):
    """Prune the archives from the bucket.

    Keyword Args:
        context (:class:`runway.cfngin.context.Context`): The context
            instance.
        session (:class:`runway.cfngin.session.Session`): The CFNgin
            session.

    """
    LOGGER.info("staticsite: cleaning up old site archives...")
    archives = []
    s3_client = session.client('s3')
    list_objects_v2_paginator = s3_client.get_paginator('list_objects_v2')
    response_iterator = list_objects_v2_paginator.paginate(
        Bucket=context.hook_data['staticsite']['artifact_bucket_name'],
        Prefix=context.hook_data['staticsite']['artifact_key_prefix']
    )

    for page in response_iterator:
        archives.extend(page.get('Contents', []))
    archives_to_prune = get_archives_to_prune(
        archives,
        context.hook_data['staticsite']
    )

    # Iterate in chunks of 1000 to match delete_objects limit
    for objects in [archives_to_prune[i:i + 1000]
                    for i in range(0, len(archives_to_prune), 1000)]:
        s3_client.delete_objects(
            Bucket=context.hook_data['staticsite']['artifact_bucket_name'],
            Delete={'Objects': [{'Key': i} for i in objects]}
        )
    return True


def auto_detect_content_type(filename):
    """Auto detects the content type based on the filename."""
    _, ext = os.path.splitext(filename)

    if ext == '.json':
        return 'application/json'

    if ext in ['.yml', '.yaml']:
        return 'text/yaml'

    return None


def get_content_type(extra_file):
    """Return the content type of the file."""
    return extra_file.get(
        'content_type',
        auto_detect_content_type(extra_file.get('name')))


def get_content(extra_file):
    """."""
    content_type = extra_file.get('content_type')
    content = extra_file.get('content')

    if content:
        if isinstance(content, (dict, list)):
            if content_type == 'application/json':
                return json.dumps(content)

            if content_type == 'text/yaml':
                return yaml.safe_dump(content)

            raise ValueError('"content_type" must be json or yaml if "content" is not a string')

        if not isinstance(content, str):
            raise TypeError('unsupported content: %s' % type(content))

    return content


def calculate_hash_of_extra_files(extra_files):
    """."""
    file_hash = hashlib.md5()

    for extra_file in sorted(extra_files, key=lambda extra_file: extra_file['name']):
        file_hash.update((extra_file['name'] + "\0").encode())

        if extra_file.get('content_type'):
            file_hash.update((extra_file['content_type'] + "\0").encode())

        if extra_file.get('content'):
            LOGGER.debug('hashing content %s', extra_file['name'])
            file_hash.update((extra_file['content'] + "\0").encode())

        if extra_file.get('file'):
            with open(extra_file['file'], "rb") as filedes:
                LOGGER.debug('hashing file %s', extra_file['file'])
                for chunk in iter(lambda: filedes.read(4096), ""):  # noqa pylint: disable=cell-var-from-loop
                    if not chunk:
                        break
                    file_hash.update(chunk)
                file_hash.update("\0".encode())

    return file_hash.hexdigest()


def get_ssm_value(session, name):
    """."""
    ssm_client = session.client('ssm')

    try:
        return ssm_client.get_parameter(Name=name)['Parameter']['Value']
    except ssm_client.exceptions.ParameterNotFound:
        return None


def set_ssm_value(session, name, value, description=''):
    """."""
    ssm_client = session.client('ssm')

    ssm_client.put_parameter(
        Name=name,
        Description=description,
        Value=value,
        Type='String',
        Overwrite=True
    )


def sync_extra_files(context, bucket, extra_files, **kwargs):
    """Sync static website extra files to S3 bucket.

    Keyword Args:

        context (:class:`runway.cfngin.context.Context`): The context
            instance.
        bucket (str): The static site bucket name.
        extra_files (List[Dict[str, str]]): List of files and file content that should be
            uploaded.

    """
    LOGGER.debug('bucket: %s', bucket)
    LOGGER.debug('extra_files: %s', json.dumps(extra_files))

    if len(extra_files) == 0:
        return []

    session = context.get_session()
    s3_client = session.client('s3')
    uploaded = []

    hash_param = kwargs.get('hash_tracking_parameter')
    hash_new = None

    # serialize content based on content type
    for extra_file in extra_files:
        filename = extra_file.get('name')
        extra_file['content_type'] = get_content_type(extra_file)
        extra_file['content'] = get_content(extra_file)

    # calculate a hash of the extra_files
    if hash_param:
        hash_param = "%sextra" % hash_param

        hash_old = get_ssm_value(session, hash_param)

        # calculate hash of content
        hash_new = calculate_hash_of_extra_files(extra_files)

        if hash_new == hash_old:
            LOGGER.info("Skipping extra files upload; latest version already deployed")
            return []

    for extra_file in extra_files:
        filename = extra_file['name']
        content_type = extra_file['content_type']
        content = extra_file['content']
        source = extra_file.get('file')

        if content:
            LOGGER.info('Uploading extra file: %s', filename)

            s3_client.put_object(
                Bucket=bucket,
                Key=filename,
                Body=content,
                ContentType=content_type
            )

            uploaded.append(filename)

        if source:
            LOGGER.info('Uploading extra file: %s as %s ', source, filename)

            extra_args = None

            if content_type:
                extra_args = {'ContentType': content_type}

            s3_client.upload_file(source, bucket, filename, ExtraArgs=extra_args)

            uploaded.append(filename)

    if hash_new:
        LOGGER.info("Updating extra files SSM parameter %s with hash %s", hash_param, hash_new)
        set_ssm_value(session, hash_param, hash_new)

    return uploaded
