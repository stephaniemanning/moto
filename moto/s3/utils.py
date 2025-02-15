import logging
import base64
import binascii
import re
import hashlib
from urllib.parse import urlparse, unquote, quote
from requests.structures import CaseInsensitiveDict
from typing import Union, Tuple
import sys
from moto.settings import S3_IGNORE_SUBDOMAIN_BUCKETNAME


log = logging.getLogger(__name__)


bucket_name_regex = re.compile(r"(.+)\.s3(.*)\.amazonaws.com")
user_settable_fields = {
    "content-md5",
    "content-language",
    "content-type",
    "content-encoding",
    "cache-control",
    "expires",
    "content-disposition",
    "x-robots-tag",
}
ARCHIVE_STORAGE_CLASSES = [
    "GLACIER",
    "DEEP_ARCHIVE",
    "GLACIER_IR",
]
STORAGE_CLASS = [
    "STANDARD",
    "REDUCED_REDUNDANCY",
    "STANDARD_IA",
    "ONEZONE_IA",
    "INTELLIGENT_TIERING",
] + ARCHIVE_STORAGE_CLASSES


def bucket_name_from_url(url):
    if S3_IGNORE_SUBDOMAIN_BUCKETNAME:
        return None
    domain = urlparse(url).netloc

    if domain.startswith("www."):
        domain = domain[4:]

    if "amazonaws.com" in domain:
        bucket_result = bucket_name_regex.search(domain)
        if bucket_result:
            return bucket_result.groups()[0]
    else:
        if "." in domain:
            return domain.split(".")[0]
        else:
            # No subdomain found.
            return None


# 'owi-common-cf', 'snippets/test.json' = bucket_and_name_from_url('s3://owi-common-cf/snippets/test.json')
def bucket_and_name_from_url(url: str) -> Union[Tuple[str, str], Tuple[None, None]]:
    prefix = "s3://"
    if url.startswith(prefix):
        bucket_name = url[len(prefix) : url.index("/", len(prefix))]
        key = url[url.index("/", len(prefix)) + 1 :]
        return bucket_name, key
    else:
        return None, None


REGION_URL_REGEX = re.compile(
    r"^https?://(s3[-\.](?P<region1>.+)\.amazonaws\.com/(.+)|"
    r"(.+)\.s3[-\.](?P<region2>.+)\.amazonaws\.com)/?"
)


def parse_region_from_url(url, use_default_region=True):
    match = REGION_URL_REGEX.search(url)
    if match:
        region = match.group("region1") or match.group("region2")
    else:
        region = "us-east-1" if use_default_region else None
    return region


def metadata_from_headers(headers):
    metadata = CaseInsensitiveDict()
    meta_regex = re.compile(r"^x-amz-meta-([a-zA-Z0-9\-_.]+)$", flags=re.IGNORECASE)
    for header in headers.keys():
        if isinstance(header, str):
            result = meta_regex.match(header)
            meta_key = None
            if result:
                # Check for extra metadata
                meta_key = result.group(0).lower()
            elif header.lower() in user_settable_fields:
                # Check for special metadata that doesn't start with x-amz-meta
                meta_key = header
            if meta_key:
                metadata[meta_key] = (
                    headers[header][0]
                    if type(headers[header]) == list
                    else headers[header]
                )
    return metadata


def clean_key_name(key_name):
    return unquote(key_name)


def undo_clean_key_name(key_name):
    return quote(key_name)


class _VersionedKeyStore(dict):

    """A simplified/modified version of Django's `MultiValueDict` taken from:
    https://github.com/django/django/blob/70576740b0bb5289873f5a9a9a4e1a26b2c330e5/django/utils/datastructures.py#L282
    """

    def __sgetitem__(self, key):
        return super().__getitem__(key)

    def pop(self, key):
        for version in self.getlist(key, []):
            version.dispose()
        super().pop(key)

    def __getitem__(self, key):
        return self.__sgetitem__(key)[-1]

    def __setitem__(self, key, value):
        try:
            current = self.__sgetitem__(key)
            current.append(value)
        except (KeyError, IndexError):
            current = [value]

        super().__setitem__(key, current)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            pass
        return default

    def getlist(self, key, default=None):
        try:
            return self.__sgetitem__(key)
        except (KeyError, IndexError):
            pass
        return default

    def setlist(self, key, list_):
        if isinstance(list_, tuple):
            list_ = list(list_)
        elif not isinstance(list_, list):
            list_ = [list_]

        for existing_version in self.getlist(key, []):
            # Dispose of any FakeKeys that we will not keep
            # We should only have FakeKeys here - but we're checking hasattr to be sure
            if existing_version not in list_ and hasattr(existing_version, "dispose"):
                existing_version.dispose()

        super().__setitem__(key, list_)

    def _iteritems(self):
        for key in self._self_iterable():
            yield key, self[key]

    def _itervalues(self):
        for key in self._self_iterable():
            yield self[key]

    def _iterlists(self):
        for key in self._self_iterable():
            yield key, self.getlist(key)

    def item_size(self):
        size = 0
        for val in self._self_iterable().values():
            size += sys.getsizeof(val)
        return size

    def _self_iterable(self):
        # to enable concurrency, return a copy, to avoid "dictionary changed size during iteration"
        # TODO: look into replacing with a locking mechanism, potentially
        return dict(self)

    items = iteritems = _iteritems
    lists = iterlists = _iterlists
    values = itervalues = _itervalues


def compute_checksum(body, algorithm):
    if algorithm == "SHA1":
        hashed_body = _hash(hashlib.sha1, (body,))
    elif algorithm == "CRC32" or algorithm == "CRC32C":
        hashed_body = f"{binascii.crc32(body)}".encode("utf-8")
    else:
        hashed_body = _hash(hashlib.sha256, (body,))
    return base64.b64encode(hashed_body)


def _hash(fn, args) -> bytes:
    try:
        return fn(*args, usedforsecurity=False).hexdigest().encode("utf-8")
    except TypeError:
        # The usedforsecurity-parameter is only available as of Python 3.9
        return fn(*args).hexdigest().encode("utf-8")
