import hashlib
import hmac
import html
import itertools
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta
from io import BytesIO, StringIO

import dateutil.parser
from flask_babel import lazy_gettext as _
from lxml import etree
from pymarc import parse_xml_to_array

from core.analytics import Analytics
from core.config import CannotLoadConfiguration
from core.coverage import BibliographicCoverageProvider
from core.metadata_layer import (
    CirculationData,
    ContributorData,
    FormatData,
    IdentifierData,
    LinkData,
    MeasurementData,
    Metadata,
    ReplacementPolicy,
    SubjectData,
)
from core.model import (
    CirculationEvent,
    Classification,
    Collection,
    Contributor,
    DataSource,
    DeliveryMechanism,
    Edition,
    ExternalIntegration,
    Hyperlink,
    Identifier,
    LicensePool,
    Measurement,
    Representation,
    Session,
    Subject,
    Timestamp,
    get_one,
    get_one_or_create,
)
from core.monitor import CollectionMonitor, IdentifierSweepMonitor, TimelineMonitor
from core.scripts import RunCollectionMonitorScript
from core.testing import DatabaseTest
from core.util.datetime_helpers import datetime_utc, strptime_utc, to_utc, utc_now
from core.util.http import HTTP
from core.util.string_helpers import base64
from core.util.xmlparser import XMLParser

from .circulation import BaseCirculationAPI, FulfillmentInfo, HoldInfo, LoanInfo
from .circulation_exceptions import *
from .selftest import HasSelfTests, SelfTestResult
from .web_publication_manifest import FindawayManifest, SpineItem


class BibliothecaAPI(BaseCirculationAPI, HasSelfTests):

    NAME = ExternalIntegration.BIBLIOTHECA
    AUTH_TIME_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"
    ARGUMENT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
    AUTHORIZATION_FORMAT = "3MCLAUTH %s:%s"

    DATETIME_HEADER = "3mcl-Datetime"
    AUTHORIZATION_HEADER = "3mcl-Authorization"
    VERSION_HEADER = "3mcl-Version"

    log = logging.getLogger("Bibliotheca API")

    DEFAULT_VERSION = "2.0"
    DEFAULT_BASE_URL = "https://partner.yourcloudlibrary.com/"

    SETTINGS = [
        {
            "key": ExternalIntegration.USERNAME,
            "label": _("Account ID"),
            "required": True,
        },
        {
            "key": ExternalIntegration.PASSWORD,
            "label": _("Account Key"),
            "required": True,
        },
        {
            "key": Collection.EXTERNAL_ACCOUNT_ID_KEY,
            "label": _("Library ID"),
            "required": True,
        },
    ] + BaseCirculationAPI.SETTINGS

    LIBRARY_SETTINGS = BaseCirculationAPI.LIBRARY_SETTINGS + [
        BaseCirculationAPI.DEFAULT_LOAN_DURATION_SETTING
    ]

    MAX_AGE = timedelta(days=730).seconds
    CAN_REVOKE_HOLD_WHEN_RESERVED = False
    SET_DELIVERY_MECHANISM_AT = None

    SERVICE_NAME = "Bibliotheca"

    # Create a lookup table between common DeliveryMechanism identifiers
    # and Overdrive format types.
    adobe_drm = DeliveryMechanism.ADOBE_DRM
    findaway_drm = DeliveryMechanism.FINDAWAY_DRM
    delivery_mechanism_to_internal_format = {
        (Representation.EPUB_MEDIA_TYPE, adobe_drm): "ePub",
        (Representation.PDF_MEDIA_TYPE, adobe_drm): "PDF",
        (None, findaway_drm): "MP3",
    }
    internal_format_to_delivery_mechanism = dict(
        [v, k] for k, v in list(delivery_mechanism_to_internal_format.items())
    )

    def __init__(self, _db, collection):

        if collection.protocol != ExternalIntegration.BIBLIOTHECA:
            raise ValueError(
                "Collection protocol is %s, but passed into BibliothecaAPI!"
                % collection.protocol
            )

        self._db = _db
        self.version = (
            collection.external_integration.setting("version").value
            or self.DEFAULT_VERSION
        )
        self.account_id = collection.external_integration.username
        self.account_key = collection.external_integration.password
        self.library_id = collection.external_account_id
        self.base_url = collection.external_integration.url or self.DEFAULT_BASE_URL

        if not self.account_id or not self.account_key or not self.library_id:
            raise CannotLoadConfiguration("Bibliotheca configuration is incomplete.")

        self.item_list_parser = ItemListParser()
        self.collection_id = collection.id

    @property
    def collection(self):
        return Collection.by_id(self._db, id=self.collection_id)

    @property
    def source(self):
        return DataSource.lookup(self._db, DataSource.BIBLIOTHECA)

    def now(self):
        """Return the current GMT time in the format 3M expects."""
        return time.strftime(self.AUTH_TIME_FORMAT, time.gmtime())

    def sign(self, method, headers, path):
        """Add appropriate headers to a request."""
        authorization, now = self.authorization(method, path)
        headers[self.DATETIME_HEADER] = now
        headers[self.VERSION_HEADER] = self.version
        headers[self.AUTHORIZATION_HEADER] = authorization

    def authorization(self, method, path):
        signature, now = self.signature(method, path)
        auth = self.AUTHORIZATION_FORMAT % (self.account_id, signature)
        return auth, now

    def signature(self, method, path):
        now = self.now()
        signature_components = [now, method, path]
        signature_string = "\n".join(signature_components)
        digest = hmac.new(
            self.account_key.encode("utf-8"),
            msg=signature_string.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        signature = base64.standard_b64encode(digest)
        return signature, now

    def full_url(self, path):
        if not path.startswith("/cirrus"):
            path = self.full_path(path)
        return urllib.parse.urljoin(self.base_url, path)

    def full_path(self, path):
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/cirrus"):
            path = "/cirrus/library/%s%s" % (self.library_id, path)
        return path

    @classmethod
    def replacement_policy(cls, _db, analytics=None):
        policy = ReplacementPolicy.from_license_source(_db)
        if analytics:
            policy.analytics = analytics
        return policy

    def request(self, path, body=None, method="GET", identifier=None, max_age=None):
        path = self.full_path(path)
        url = self.full_url(path)
        if method == "GET":
            headers = {"Accept": "application/xml"}
        else:
            headers = {"Content-Type": "application/xml"}
        self.sign(method, headers, path)
        # print headers
        # self.log.debug("3M request: %s %s", method, url)
        if max_age and method == "GET":
            representation, cached = Representation.get(
                self._db,
                url,
                extra_request_headers=headers,
                do_get=self._simple_http_get,
                max_age=max_age,
                exception_handler=Representation.reraise_exception,
                timeout=60,
            )
            content = representation.content
            return content
        else:
            return self._request_with_timeout(
                method,
                url,
                data=body,
                headers=headers,
                allow_redirects=False,
                timeout=60,
            )

    def get_bibliographic_info_for(self, editions, max_age=None):
        results = dict()
        for edition in editions:
            identifier = edition.primary_identifier
            metadata = self.bibliographic_lookup(identifier, max_age)
            if metadata:
                results[identifier] = (edition, metadata)
        return results

    def marc_request(self, start, end, offset=1, limit=50):
        """Make an HTTP request to look up the MARC records for books purchased
        between two given dates.

        :param start: A datetime to start looking for purchases.
        :param end: A datetime to stop looking for purchases.
        :param offset: An offset used to paginate results.
        :param limit: A limit used to paginate results.
        :raise: An appropriate exception if the request did not return
          MARC records.
        :yield: A list of MARC records.
        """
        start = start.strftime(self.ARGUMENT_TIME_FORMAT)
        end = end.strftime(self.ARGUMENT_TIME_FORMAT)
        url = "data/marc?startdate=%s&enddate=%s&offset=%d&limit=%d" % (
            start,
            end,
            offset,
            limit,
        )
        response = self.request(url)
        if response.status_code != 200:
            raise ErrorParser().process_all(response.content)
        for record in parse_xml_to_array(BytesIO(response.content)):
            yield record

    def bibliographic_lookup_request(self, identifiers):
        """Make an HTTP request to look up current bibliographic and
        circulation information for the given `identifiers`.

        :param identifiers: Strings containing Bibliotheca identifiers.
        :return: A string containing an XML document, or None if there was
           an error not handled as an exception.
        """
        url = "/items/" + ",".join(identifiers)
        response = self.request(url)
        return response.content

    def bibliographic_lookup(self, identifiers):
        """Look up current bibliographic and circulation information for the
        given `identifiers`.

        :param identifiers: A list containing either Identifier
            objects or Bibliotheca identifier strings.
        """
        if any(isinstance(identifiers, x) for x in (Identifier, str)):
            identifiers = [identifiers]
        identifier_strings = []
        for i in identifiers:
            if isinstance(i, Identifier):
                i = i.identifier
            identifier_strings.append(i)

        data = self.bibliographic_lookup_request(identifier_strings)
        return [metadata for metadata in self.item_list_parser.parse(data)]

    def _request_with_timeout(self, method, url, *args, **kwargs):
        """This will be overridden in MockBibliothecaAPI."""
        return HTTP.request_with_timeout(method, url, *args, **kwargs)

    def _simple_http_get(self, url, headers, *args, **kwargs):
        """This will be overridden in MockBibliothecaAPI."""
        return Representation.simple_http_get(url, headers, *args, **kwargs)

    def external_integration(self, _db):
        return self.collection.external_integration

    def _run_self_tests(self, _db):
        def _count_events():
            now = utc_now()
            five_minutes_ago = now - timedelta(minutes=5)
            count = len(list(self.get_events_between(five_minutes_ago, now)))
            return "Found %d event(s)" % count

        yield self.run_test(
            "Asking for circulation events for the last five minutes", _count_events
        )

        for result in self.default_patrons(self.collection):
            if isinstance(result, SelfTestResult):
                yield result
                continue
            library, patron, pin = result

            def _count_activity():
                result = self.patron_activity(patron, pin)
                return "Found %d loans/holds" % len(result)

            yield self.run_test(
                "Checking activity for test patron for library %s" % library.name,
                _count_activity,
            )

    def get_events_between(self, start, end, cache_result=False, no_events_error=False):
        """Return event objects for events between the given times."""
        start = start.strftime(self.ARGUMENT_TIME_FORMAT)
        end = end.strftime(self.ARGUMENT_TIME_FORMAT)
        url = "data/cloudevents?startdate=%s&enddate=%s" % (start, end)
        if cache_result:
            max_age = self.MAX_AGE
        else:
            max_age = None
        response = self.request(url, max_age=max_age)
        if cache_result:
            self._db.commit()
        try:
            events = EventParser().process_all(response.content, no_events_error)
        except Exception as e:
            self.log.error(
                "Error parsing Bibliotheca response content: %s",
                response.content,
                exc_info=e,
            )
            raise e
        return events

    def update_availability(self, licensepool):
        """Update the availability information for a single LicensePool."""
        monitor = BibliothecaCirculationSweep(
            self._db, licensepool.collection, api_class=self
        )
        return monitor.process_items([licensepool.identifier])

    def _patron_activity_request(self, patron):
        patron_id = patron.authorization_identifier
        path = "circulation/patron/%s" % patron_id
        return self.request(path)

    def patron_activity(self, patron, pin):
        response = self._patron_activity_request(patron)
        collection = self.collection
        return PatronCirculationParser(self.collection).process_all(response.content)

    TEMPLATE = "<%(request_type)s><ItemId>%(item_id)s</ItemId><PatronId>%(patron_id)s</PatronId></%(request_type)s>"

    def checkout(self, patron_obj, patron_password, licensepool, delivery_mechanism):

        """Check out a book on behalf of a patron.

        :param patron_obj: a Patron object for the patron who wants
            to check out the book.

        :param patron_password: The patron's alleged password.  Not used here
            since Bibliotheca trusts Simplified to do the check ahead of time.

        :param licensepool: LicensePool for the book to be checked out.

        :return: a LoanInfo object
        """
        bibliotheca_id = licensepool.identifier.identifier
        patron_identifier = patron_obj.authorization_identifier
        args = dict(
            request_type="CheckoutRequest",
            item_id=bibliotheca_id,
            patron_id=patron_identifier,
        )
        body = self.TEMPLATE % args
        response = self.request("checkout", body, method="PUT")
        if response.status_code == 201:
            # New loan
            start_date = utc_now()
        elif response.status_code == 200:
            # Old loan -- we don't know the start date
            start_date = None
        else:
            # Error condition.
            error = ErrorParser().process_all(response.content)
            if isinstance(error, AlreadyCheckedOut):
                # It's already checked out. No problem.
                pass
            else:
                raise error

        # At this point we know we have a loan.
        loan_expires = CheckoutResponseParser().process_all(response.content)
        loan = LoanInfo(
            licensepool.collection,
            DataSource.BIBLIOTHECA,
            licensepool.identifier.type,
            licensepool.identifier.identifier,
            start_date=None,
            end_date=loan_expires,
        )
        return loan

    def fulfill(self, patron, password, pool, internal_format, **kwargs):
        """Get the actual resource file to the patron.

        :param kwargs: A container for standard arguments to fulfill()
           which are not relevant to this implementation.

        :return: a FulfillmentInfo object.
        """
        media_type, drm_scheme = self.internal_format_to_delivery_mechanism.get(
            internal_format, internal_format
        )
        if drm_scheme == DeliveryMechanism.FINDAWAY_DRM:
            fulfill_method = self.get_audio_fulfillment_file
            content_transformation = self.findaway_license_to_webpub_manifest
        else:
            fulfill_method = self.get_fulfillment_file
            content_transformation = None
        response = fulfill_method(
            patron.authorization_identifier, pool.identifier.identifier
        )
        content = response.content
        content_type = None
        if content_transformation:
            try:
                content_type, content = content_transformation(pool, content)
            except Exception as e:
                self.log.error(
                    "Error transforming fulfillment document: %s",
                    response.content,
                    exc_info=e,
                )
        return FulfillmentInfo(
            pool.collection,
            DataSource.BIBLIOTHECA,
            pool.identifier.type,
            pool.identifier.identifier,
            content_link=None,
            content_type=content_type or response.headers.get("Content-Type"),
            content=content,
            content_expires=None,
        )

    def get_fulfillment_file(self, patron_id, bibliotheca_id):
        args = dict(
            request_type="ACSMRequest", item_id=bibliotheca_id, patron_id=patron_id
        )
        body = self.TEMPLATE % args
        return self.request("GetItemACSM", body, method="PUT")

    def get_audio_fulfillment_file(self, patron_id, bibliotheca_id):
        args = dict(
            request_type="AudioFulfillmentRequest",
            item_id=bibliotheca_id,
            patron_id=patron_id,
        )
        body = self.TEMPLATE % args
        return self.request("GetItemAudioFulfillment", body, method="POST")

    def checkin(self, patron, pin, licensepool):
        patron_id = patron.authorization_identifier
        item_id = licensepool.identifier.identifier
        args = dict(request_type="CheckinRequest", item_id=item_id, patron_id=patron_id)
        body = self.TEMPLATE % args
        return self.request("checkin", body, method="PUT")

    def place_hold(self, patron, pin, licensepool, hold_notification_email=None):
        """Place a hold.

        :return: a HoldInfo object.
        """
        patron_id = patron.authorization_identifier
        item_id = licensepool.identifier.identifier
        args = dict(
            request_type="PlaceHoldRequest", item_id=item_id, patron_id=patron_id
        )
        body = self.TEMPLATE % args
        response = self.request("placehold", body, method="PUT")
        # The response comes in as a byte string that we must
        # convert into a string.
        response_content = None
        if response.content:
            response_content = response.content.decode("utf-8")
        if response.status_code in (200, 201):
            start_date = utc_now()
            end_date = HoldResponseParser().process_all(response_content)
            return HoldInfo(
                licensepool.collection,
                DataSource.BIBLIOTHECA,
                licensepool.identifier.type,
                licensepool.identifier.identifier,
                start_date=start_date,
                end_date=end_date,
                hold_position=None,
            )
        else:
            if not response_content:
                raise CannotHold()
            error = ErrorParser().process_all(response_content)
            if isinstance(error, Exception):
                raise error
            else:
                raise CannotHold(error)

    def release_hold(self, patron, pin, licensepool):
        patron_id = patron.authorization_identifier
        item_id = licensepool.identifier.identifier
        args = dict(
            request_type="CancelHoldRequest", item_id=item_id, patron_id=patron_id
        )
        body = self.TEMPLATE % args
        response = self.request("cancelhold", body, method="PUT")
        if response.status_code in (200, 404):
            return True
        else:
            raise CannotReleaseHold()

    @classmethod
    def findaway_license_to_webpub_manifest(cls, license_pool, findaway_license):
        """Convert a Bibliotheca license document to a FindawayManifest
        suitable for serving to a mobile client.

        :param license_pool: A LicensePool for the title in question.
            This will be used to fill in basic bibliographic information.

        :param findaway_license: A string containing a Findaway
            license document via Bibliotheca, or a dictionary
            representing such a document loaded into JSON form.
        """
        if isinstance(findaway_license, (bytes, str)):
            findaway_license = json.loads(findaway_license)

        kwargs = {}
        for findaway_extension in [
            "accountId",
            "checkoutId",
            "fulfillmentId",
            "licenseId",
            "sessionKey",
        ]:
            value = findaway_license.get(findaway_extension, None)
            kwargs[findaway_extension] = value

        # Create the SpineItem objects.
        audio_format = findaway_license.get("format")
        if audio_format == "MP3":
            part_media_type = Representation.MP3_MEDIA_TYPE
        else:
            logging.error("Unknown Findaway audio format encountered: %s", audio_format)
            part_media_type = None

        spine_items = []
        for part in findaway_license.get("items"):
            title = part.get("title")

            # TODO: Incoming duration appears to be measured in
            # milliseconds. This assumption makes our example
            # audiobook take about 7.9 hours, and no other reasonable
            # assumption is in the right order of magnitude. But this
            # needs to be explicitly verified.
            duration = part.get("duration", 0) / 1000.0

            part_number = int(part.get("part", 0))

            sequence = int(part.get("sequence", 0))

            spine_items.append(SpineItem(title, duration, part_number, sequence))

        # Create a FindawayManifest object and then convert it
        # to a string.
        manifest = FindawayManifest(
            license_pool=license_pool, spine_items=spine_items, **kwargs
        )

        return DeliveryMechanism.FINDAWAY_DRM, str(manifest)


class DummyBibliothecaAPIResponse(object):
    def __init__(self, response_code, headers, content):
        self.status_code = response_code
        self.headers = headers
        self.content = content


class MockBibliothecaAPI(BibliothecaAPI):
    @classmethod
    def mock_collection(self, _db, name="Test Bibliotheca Collection"):
        """Create a mock Bibliotheca collection for use in tests."""
        library = DatabaseTest.make_default_library(_db)
        collection, ignore = get_one_or_create(
            _db,
            Collection,
            name=name,
            create_method_kwargs=dict(
                external_account_id="c",
            ),
        )
        integration = collection.create_external_integration(
            protocol=ExternalIntegration.BIBLIOTHECA
        )
        integration.username = "a"
        integration.password = "b"
        integration.url = "http://bibliotheca.test"
        library.collections.append(collection)
        return collection

    def __init__(self, _db, collection, *args, **kwargs):
        self.responses = []
        self.requests = []
        super(MockBibliothecaAPI, self).__init__(_db, collection, *args, **kwargs)

    def now(self):
        """Return an unvarying time in the format Bibliotheca expects."""
        return datetime.strftime(datetime(2016, 1, 1), self.AUTH_TIME_FORMAT)

    def queue_response(self, status_code, headers={}, content=None):
        from core.testing import MockRequestsResponse

        self.responses.insert(0, MockRequestsResponse(status_code, headers, content))

    def _request_with_timeout(self, method, url, *args, **kwargs):
        """Simulate HTTP.request_with_timeout."""
        self.requests.append([method, url, args, kwargs])
        response = self.responses.pop()
        return HTTP._process_response(
            url,
            response,
            kwargs.get("allowed_response_codes"),
            kwargs.get("disallowed_response_codes"),
        )

    def _simple_http_get(self, url, headers, *args, **kwargs):
        """Simulate Representation.simple_http_get."""
        response = self._request_with_timeout("GET", url, *args, **kwargs)
        return response.status_code, response.headers, response.content


class ItemListParser(XMLParser):

    DATE_FORMAT = "%Y-%m-%d"
    YEAR_FORMAT = "%Y"

    NAMESPACES = {}

    unescape_entity_references = html.unescape

    def parse(self, xml):
        for i in self.process_all(xml, "//Item"):
            yield i

    parenthetical = re.compile(" \([^)]+\)$")

    format_data_for_bibliotheca_format = {
        "EPUB": (Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM),
        "EPUB3": (Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM),
        "PDF": (Representation.PDF_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM),
        "MP3": (None, DeliveryMechanism.FINDAWAY_DRM),
    }

    @classmethod
    def contributors_from_string(cls, string, role=Contributor.AUTHOR_ROLE):
        contributors = []
        if not string:
            return contributors

        # Contributors may have two levels of entity reference escaping,
        # one of which will have already been handled by the initial parse.
        # We handle the potential need for a second unescaping here.
        string = cls.unescape_entity_references(string)

        for sort_name in string.split(";"):
            sort_name = cls.parenthetical.sub("", sort_name.strip())
            contributors.append(
                ContributorData(sort_name=sort_name.strip(), roles=[role])
            )
        return contributors

    @classmethod
    def parse_genre_string(self, s):
        genres = []
        if not s:
            return genres
        for i in s.split(","):
            i = i.strip()
            if not i:
                continue
            i = (
                i.replace("&amp;amp;", "&amp;")
                .replace("&amp;", "&")
                .replace("&#39;", "'")
            )
            genres.append(
                SubjectData(
                    Subject.BISAC,
                    None,
                    i,
                    weight=Classification.TRUSTED_DISTRIBUTOR_WEIGHT,
                )
            )
        return genres

    def process_one(self, tag, namespaces):
        """Turn an <item> tag into a Metadata and an encompassed CirculationData
        objects, and return the Metadata."""

        def value(bibliotheca_key):
            return self.text_of_optional_subtag(tag, bibliotheca_key)

        links = dict()
        identifiers = dict()
        subjects = []

        primary_identifier = IdentifierData(Identifier.BIBLIOTHECA_ID, value("ItemId"))

        identifiers = []
        for key in ("ISBN13", "PhysicalISBN"):
            v = value(key)
            if v:
                identifiers.append(IdentifierData(Identifier.ISBN, v))

        subjects = self.parse_genre_string(value("Genre"))

        title = value("Title")
        subtitle = value("SubTitle")
        publisher = value("Publisher")
        language = value("Language")

        authors = list(self.contributors_from_string(value("Authors")))
        narrators = list(
            self.contributors_from_string(value("Narrator"), Contributor.NARRATOR_ROLE)
        )

        published_date = None
        published = value("PubDate")
        if published:
            formats = [self.DATE_FORMAT, self.YEAR_FORMAT]
        else:
            published = value("PubYear")
            formats = [self.YEAR_FORMAT]

        for format in formats:
            try:
                published_date = strptime_utc(published, format)
            except ValueError as e:
                pass

        links = []
        description = value("Description")
        if description:
            links.append(LinkData(rel=Hyperlink.DESCRIPTION, content=description))

        # Presume all images from Bibliotheca are JPEG.
        media_type = Representation.JPEG_MEDIA_TYPE
        cover_url = value("CoverLinkURL").replace("&amp;", "&")
        cover_link = LinkData(
            rel=Hyperlink.IMAGE, href=cover_url, media_type=media_type
        )

        # Unless the URL format has drastically changed, we should be
        # able to generate a thumbnail URL based on the full-size
        # cover URL found in the response document.
        #
        # NOTE: this is an undocumented feature of the Bibliotheca API
        # which was discovered by investigating the BookLinkURL.
        if "/delivery/img" in cover_url:
            thumbnail_url = cover_url + "&size=NORMAL"
            thumbnail = LinkData(
                rel=Hyperlink.THUMBNAIL_IMAGE, href=thumbnail_url, media_type=media_type
            )
            cover_link.thumbnail = thumbnail
        links.append(cover_link)

        alternate_url = value("BookLinkURL").replace("&amp;", "&")
        links.append(LinkData(rel="alternate", href=alternate_url))

        measurements = []
        pages = value("NumberOfPages")
        if pages:
            pages = int(pages)
            measurements.append(
                MeasurementData(quantity_measured=Measurement.PAGE_COUNT, value=pages)
            )

        circulation, medium = self._make_circulation_data(
            tag, namespaces, primary_identifier
        )

        metadata = Metadata(
            data_source=DataSource.BIBLIOTHECA,
            title=title,
            subtitle=subtitle,
            language=language,
            medium=medium,
            publisher=publisher,
            published=published_date,
            primary_identifier=primary_identifier,
            identifiers=identifiers,
            subjects=subjects,
            contributors=authors + narrators,
            measurements=measurements,
            links=links,
            circulation=circulation,
        )
        return metadata

    def _make_circulation_data(self, tag, namespaces, primary_identifier):
        """Parse out a CirculationData containing current circulation
        and formatting information.
        """

        def value(bibliotheca_key):
            return self.text_of_subtag(tag, bibliotheca_key)

        def intvalue(key):
            return self.int_of_subtag(tag, key)

        book_format = value("BookFormat")
        medium, formats = self.internal_formats(book_format)

        licenses_owned = intvalue("TotalCopies")
        try:
            licenses_available = intvalue("AvailableCopies")
        except IndexError:
            logging.warn(
                "No information on available copies for %s",
                primary_identifier.identifier,
            )
            licenses_available = 0

        patrons_in_hold_queue = intvalue("OnHoldCount")
        licenses_reserved = 0

        circulation = CirculationData(
            data_source=DataSource.BIBLIOTHECA,
            primary_identifier=primary_identifier,
            licenses_owned=licenses_owned,
            licenses_available=licenses_available,
            licenses_reserved=licenses_reserved,
            patrons_in_hold_queue=patrons_in_hold_queue,
            formats=formats,
        )
        return circulation, medium

    @classmethod
    def internal_formats(cls, book_format):
        """Convert the term Bibliotheca uses to refer to a book
        format into a (medium [formats]) 2-tuple.
        """
        medium = Edition.BOOK_MEDIUM
        format = None
        if book_format not in cls.format_data_for_bibliotheca_format:
            logging.error("Unrecognized BookFormat: %s", book_format)
            return medium, []

        content_type, drm_scheme = cls.format_data_for_bibliotheca_format[book_format]

        format = FormatData(content_type=content_type, drm_scheme=drm_scheme)
        if book_format == "MP3":
            medium = Edition.AUDIO_MEDIUM
        else:
            medium = Edition.BOOK_MEDIUM
        return medium, [format]


class BibliothecaParser(XMLParser):

    INPUT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

    def parse_date(self, value):
        """Parse the string Bibliotheca sends as a date.

        Usually this is a string in INPUT_TIME_FORMAT, but it might be None.
        """
        if not value:
            value = None
        else:
            try:
                value = strptime_utc(value, self.INPUT_TIME_FORMAT)
            except ValueError as e:
                logging.error(
                    'Unable to parse Bibliotheca date: "%s"', value, exc_info=e
                )
                value = None
        return to_utc(value)

    def date_from_subtag(self, tag, key, required=True):
        if required:
            value = self.text_of_subtag(tag, key)
        else:
            value = self.text_of_optional_subtag(tag, key)
        return self.parse_date(value)


class BibliothecaException(Exception):
    pass


class WorkflowException(BibliothecaException):
    def __init__(self, actual_status, statuses_that_would_work):
        self.actual_status = actual_status
        self.statuses_that_would_work = statuses_that_would_work

    def __str__(self):
        return "Book status is %s, must be: %s" % (
            self.actual_status,
            ", ".join(self.statuses_that_would_work),
        )


class ErrorParser(BibliothecaParser):
    """Turn an error document from the Bibliotheca web service into a CheckoutException"""

    wrong_status = re.compile(
        "the patron document status was ([^ ]+) and not one of ([^ ]+)"
    )

    loan_limit_reached = re.compile("Patron cannot loan more than [0-9]+ document")

    hold_limit_reached = re.compile("Patron cannot have more than [0-9]+ hold")

    error_mapping = {
        "The patron does not have the book on hold": NotOnHold,
        "The patron has no eBooks checked out": NotCheckedOut,
    }

    def process_all(self, string):
        try:
            for i in super(ErrorParser, self).process_all(string, "//Error"):
                return i
        except Exception as e:
            # The server sent us an error with an incorrect or
            # nonstandard syntax.
            return RemoteInitiatedServerError(string, BibliothecaAPI.SERVICE_NAME)

        # We were not able to interpret the result as an error.
        # The most likely cause is that the Bibliotheca app server is down.
        return RemoteInitiatedServerError(
            "Unknown error",
            BibliothecaAPI.SERVICE_NAME,
        )

    def process_one(self, error_tag, namespaces):
        message = self.text_of_optional_subtag(error_tag, "Message")
        if not message:
            return RemoteInitiatedServerError(
                "Unknown error",
                BibliothecaAPI.SERVICE_NAME,
            )

        if message in self.error_mapping:
            return self.error_mapping[message](message)
        if message in ("Authentication failed", "Unknown error"):
            # 'Unknown error' is an unknown error on the Bibliotheca side.
            #
            # 'Authentication failed' could _in theory_ be an error on
            # our side, but if authentication is set up improperly we
            # actually get a 401 and no body. When we get a real error
            # document with 'Authentication failed', it's always a
            # transient error on the Bibliotheca side. Possibly some
            # authentication internal to Bibliotheca has failed? Anyway, it
            # happens relatively frequently.
            return RemoteInitiatedServerError(message, BibliothecaAPI.SERVICE_NAME)

        m = self.loan_limit_reached.search(message)
        if m:
            return PatronLoanLimitReached(message)

        m = self.hold_limit_reached.search(message)
        if m:
            return PatronHoldLimitReached(message)

        m = self.wrong_status.search(message)
        if not m:
            return BibliothecaException(message)
        actual, expected = m.groups()
        expected = expected.split(",")

        if actual == "CAN_WISH":
            return NoLicenses(message)

        if "CAN_LOAN" in expected and actual == "CAN_HOLD":
            return NoAvailableCopies(message)

        if "CAN_LOAN" in expected and actual == "HOLD":
            return AlreadyOnHold(message)

        if "CAN_LOAN" in expected and actual == "LOAN":
            return AlreadyCheckedOut(message)

        if "CAN_HOLD" in expected and actual == "CAN_LOAN":
            return CurrentlyAvailable(message)

        if "CAN_HOLD" in expected and actual == "HOLD":
            return AlreadyOnHold(message)

        if "CAN_HOLD" in expected:
            return CannotHold(message)

        if "CAN_LOAN" in expected:
            return CannotLoan(message)

        return BibliothecaException(message)


class PatronCirculationParser(BibliothecaParser):

    """Parse Bibliotheca's patron circulation status document into a list of
    LoanInfo and HoldInfo objects.
    """

    id_type = Identifier.BIBLIOTHECA_ID

    def __init__(self, collection, *args, **kwargs):
        super(PatronCirculationParser, self).__init__(*args, **kwargs)
        self.collection = collection

    def process_all(self, string):
        parser = etree.XMLParser()
        # If the data is an HTTP response, it is a bytestring and
        # must be converted before it is parsed.
        if isinstance(string, bytes):
            string = string.decode("utf-8")
        root = etree.parse(StringIO(string), parser)
        sup = super(PatronCirculationParser, self)
        loans = sup.process_all(root, "//Checkouts/Item", handler=self.process_one_loan)
        holds = sup.process_all(root, "//Holds/Item", handler=self.process_one_hold)
        reserves = sup.process_all(
            root, "//Reserves/Item", handler=self.process_one_reserve
        )

        everything = itertools.chain(loans, holds, reserves)
        return [x for x in everything if x]

    def process_one_loan(self, tag, namespaces):
        return self.process_one(tag, namespaces, LoanInfo)

    def process_one_hold(self, tag, namespaces):
        return self.process_one(tag, namespaces, HoldInfo)

    def process_one_reserve(self, tag, namespaces):
        hold_info = self.process_one(tag, namespaces, HoldInfo)
        hold_info.hold_position = 0
        return hold_info

    def process_one(self, tag, namespaces, source_class):
        if not tag.xpath("ItemId"):
            # This happens for events associated with books
            # no longer in our collection.
            return None

        def datevalue(key):
            value = self.text_of_subtag(tag, key)
            return strptime_utc(value, BibliothecaAPI.ARGUMENT_TIME_FORMAT)

        identifier = self.text_of_subtag(tag, "ItemId")
        start_date = datevalue("EventStartDateInUTC")
        end_date = datevalue("EventEndDateInUTC")
        a = [
            self.collection,
            DataSource.BIBLIOTHECA,
            self.id_type,
            identifier,
            start_date,
            end_date,
        ]
        if source_class is HoldInfo:
            hold_position = self.int_of_subtag(tag, "Position")
            a.append(hold_position)
        else:
            # Fulfillment info -- not available from this API
            a.append(None)
        return source_class(*a)


class DateResponseParser(BibliothecaParser):
    """Extract a date from a response."""

    RESULT_TAG_NAME = None
    DATE_TAG_NAME = None

    def process_all(self, string):
        parser = etree.XMLParser()
        # If the data is an HTTP response, it is a bytestring and
        # must be converted before it is parsed.
        if isinstance(string, bytes):
            string = string.decode("utf-8")
        root = etree.parse(StringIO(string), parser)
        m = root.xpath("/%s/%s" % (self.RESULT_TAG_NAME, self.DATE_TAG_NAME))
        if not m:
            return None
        due_date = m[0].text
        if not due_date:
            return None
        return strptime_utc(due_date, EventParser.INPUT_TIME_FORMAT)


class CheckoutResponseParser(DateResponseParser):

    """Extract due date from a checkout response."""

    RESULT_TAG_NAME = "CheckoutResult"
    DATE_TAG_NAME = "DueDateInUTC"


class HoldResponseParser(DateResponseParser):

    """Extract availability date from a hold response."""

    RESULT_TAG_NAME = "PlaceHoldResult"
    DATE_TAG_NAME = "AvailabilityDateInUTC"


class EventParser(BibliothecaParser):

    """Parse Bibliotheca's event file format into our native event objects."""

    EVENT_SOURCE = "Bibliotheca"

    SET_DELIVERY_MECHANISM_AT = BaseCirculationAPI.BORROW_STEP

    # Map Bibliotheca's event names to our names.
    EVENT_NAMES = {
        "CHECKOUT": CirculationEvent.DISTRIBUTOR_CHECKOUT,
        "CHECKIN": CirculationEvent.DISTRIBUTOR_CHECKIN,
        "HOLD": CirculationEvent.DISTRIBUTOR_HOLD_PLACE,
        "RESERVED": CirculationEvent.DISTRIBUTOR_AVAILABILITY_NOTIFY,
        "PURCHASE": CirculationEvent.DISTRIBUTOR_LICENSE_ADD,
        "REMOVED": CirculationEvent.DISTRIBUTOR_LICENSE_REMOVE,
    }

    def process_all(self, string, no_events_error=False):
        has_events = False
        for i in super(EventParser, self).process_all(string, "//CloudLibraryEvent"):
            yield i
            has_events = True

        # If we are catching up on events and we expect to have a time
        # period where there are no events, we don't want to consider that
        # action as an error. By default, not having events is not
        # considered to be an error.
        if not has_events and no_events_error:
            # An empty list of events may mean nothing happened, or it
            # may indicate an unreported server-side error. To be
            # safe, we'll treat this as a server-initiated error
            # condition. If this is just a slow day, normal behavior
            # will resume as soon as something happens.
            raise RemoteInitiatedServerError(
                "No events returned from server. This may not be an error, but treating it as one to be safe.",
                BibliothecaAPI.SERVICE_NAME,
            )

    def process_one(self, tag, namespaces):
        isbn = self.text_of_subtag(tag, "ISBN")
        bibliotheca_id = self.text_of_subtag(tag, "ItemId")
        patron_id = self.text_of_optional_subtag(tag, "PatronId")

        start_time = self.date_from_subtag(tag, "EventStartDateTimeInUTC")
        end_time = self.date_from_subtag(tag, "EventEndDateTimeInUTC", required=False)

        bibliotheca_event_type = self.text_of_subtag(tag, "EventType")
        internal_event_type = self.EVENT_NAMES[bibliotheca_event_type]

        return (
            bibliotheca_id,
            isbn,
            patron_id,
            start_time,
            end_time,
            internal_event_type,
        )


class BibliothecaCirculationSweep(IdentifierSweepMonitor):
    """Check on the current circulation status of each Bibliotheca book in our
    collection.

    In some cases this will lead to duplicate events being logged,
    because this monitor and the main Bibliotheca circulation monitor will
    count the same event.  However it will greatly improve our current
    view of our Bibliotheca circulation, which is more important.

    If Bibliotheca has updated its metadata for a book, that update will
    also take effect during the circulation sweep.

    If a Bibliotheca license has expired, and we didn't hear about it for
    whatever reason, we'll find out about it here, because Bibliotheca
    will act like they never heard of it.
    """

    SERVICE_NAME = "Bibliotheca Circulation Sweep"
    DEFAULT_BATCH_SIZE = 25
    PROTOCOL = ExternalIntegration.BIBLIOTHECA

    def __init__(self, _db, collection, api_class=BibliothecaAPI, **kwargs):
        _db = Session.object_session(collection)
        super(BibliothecaCirculationSweep, self).__init__(_db, collection, **kwargs)
        if isinstance(api_class, BibliothecaAPI):
            self.api = api_class
        else:
            self.api = api_class(_db, collection)
        self.replacement_policy = BibliothecaAPI.replacement_policy(_db)
        self.analytics = self.replacement_policy.analytics

    def process_items(self, identifiers):
        identifiers_by_bibliotheca_id = dict()
        bibliotheca_ids = set()
        for identifier in identifiers:
            bibliotheca_ids.add(identifier.identifier)
            identifiers_by_bibliotheca_id[identifier.identifier] = identifier

        identifiers_not_mentioned_by_bibliotheca = set(identifiers)
        now = utc_now()
        for metadata in self.api.bibliographic_lookup(bibliotheca_ids):
            self._process_metadata(
                metadata,
                identifiers_by_bibliotheca_id,
                identifiers_not_mentioned_by_bibliotheca,
            )

        # At this point there may be some license pools left over
        # that Bibliotheca doesn't know about.  This is a pretty reliable
        # indication that we no longer own any licenses to the
        # book.
        for identifier in identifiers_not_mentioned_by_bibliotheca:
            pools = [
                lp
                for lp in identifier.licensed_through
                if lp.data_source.name == DataSource.BIBLIOTHECA
                and lp.collection == self.collection
            ]
            if pools:
                [pool] = pools
            else:
                continue
            if pool.licenses_owned > 0:
                self.log.warn("Removing %s from circulation.", identifier.identifier)
            pool.update_availability(0, 0, 0, 0, self.analytics, as_of=now)

    def _process_metadata(
        self,
        metadata,
        identifiers_by_bibliotheca_id,
        identifiers_not_mentioned_by_bibliotheca,
    ):
        """Process a single Metadata object (containing CirculationData)
        retrieved from Bibliotheca.
        """
        bibliotheca_id = metadata.primary_identifier.identifier
        identifier = identifiers_by_bibliotheca_id[bibliotheca_id]
        if identifier in identifiers_not_mentioned_by_bibliotheca:
            # Bibliotheca mentioned this identifier. Remove it from
            # this list so we know the title is still in the collection.
            identifiers_not_mentioned_by_bibliotheca.remove(identifier)

        edition, is_new = metadata.edition(self._db)
        pool, is_new = metadata.circulation.license_pool(self._db, self.collection)
        if is_new:
            # We didn't have a license pool for this work. That
            # shouldn't happen--how did we know about the
            # identifier?--but now we do.
            for library in self.collection.libraries:
                self.analytics.collect_event(
                    library, pool, CirculationEvent.DISTRIBUTOR_TITLE_ADD, utc_now()
                )
        edition, ignore = metadata.apply(
            edition, collection=self.collection, replace=self.replacement_policy
        )


class BibliothecaTimelineMonitor(CollectionMonitor, TimelineMonitor):
    """Common superclass for our two TimelineMonitors."""

    PROTOCOL = ExternalIntegration.BIBLIOTHECA
    LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

    def __init__(self, _db, collection, api_class=BibliothecaAPI, analytics=None):
        """Initializer.

        :param _db: Database session object.

        :param collection: Collection for which this monitor operates.

        :param api_class: API class or an instance thereof for this monitor.
        :type api_class: Union[Type[BibliothecaAPI], BibliothecaAPI]

        :param analytics: An optional Analytics object.
        :type analytics: Optional[Analytics]
        """
        self.analytics = analytics or Analytics(_db)
        super(BibliothecaTimelineMonitor, self).__init__(_db, collection)
        if isinstance(api_class, BibliothecaAPI):
            # We were given an actual API object. Just use it.
            self.api = api_class
        else:
            self.api = api_class(_db, collection)
        self.replacement_policy = BibliothecaAPI.replacement_policy(_db, self.analytics)
        self.bibliographic_coverage_provider = BibliothecaBibliographicCoverageProvider(
            collection, self.api, replacement_policy=self.replacement_policy
        )


class BibliothecaPurchaseMonitor(BibliothecaTimelineMonitor):
    """Track purchases of licenses from Bibliotheca.

    Most TimelineMonitors monitor the timeline starting at whatever
    time they're first run. But it's crucial that this monitor start
    at or before the first day on which a book was added to this
    collection, even if that date was years in the past. That's
    because this monitor may be the only time we hear about a
    particular book.

    Because of this, this monitor has a very old DEFAULT_START_TIME
    and special capabilities for customizing the start_time to go back
    even further.
    """

    SERVICE_NAME = "Bibliotheca Purchase Monitor"
    DEFAULT_START_TIME = datetime_utc(2014, 1, 1)

    def __init__(
        self,
        _db,
        collection,
        api_class=BibliothecaAPI,
        default_start=None,
        override_timestamp=False,
        analytics=None,
    ):
        """Initializer.

        :param _db: Database session object.

        :param collection: Collection for which this monitor operates.

        :param api_class: API class or an instance thereof for this monitor.
        :type api_class: Union[Type[BibliothecaAPI], BibliothecaAPI]

        :param default_start: A default date/time at which to start
            requesting events. It should be specified as a `datetime` or
            an ISO 8601 string. If not provided, the monitor's calculated
            intrinsic default will be used.
        :type default_start: Optional[Union[datetime, basestring]]

        :param analytics: An optional Analytics object.
        :type analytics: Optional[Analytics]

        :param override_timestamp: Boolean indicating whether
            `default_start` should take precedence over the timestamp
            for an already initialized monitor.
        :type override_timestamp: bool
        """
        super(BibliothecaPurchaseMonitor, self).__init__(
            _db=_db, collection=collection, api_class=api_class, analytics=analytics
        )

        # We should only force the use of `default_start` as the actual
        # start time if it was passed in.
        self.override_timestamp = override_timestamp if default_start else False
        # A specified `default_start` takes precedence over the
        # monitor's intrinsic default start date/time.
        self.default_start_time = self._optional_iso_date(
            default_start
        ) or self._intrinsic_start_time(_db)

    def _optional_iso_date(self, date):
        """Return the date in `datetime` format.

        :param date: A date/time value, specified as either an ISO 8601
            string or as a `datetime`.
        :type date: Optional[Union[datetime, str]]

        :return: Optional datetime.
        :rtype: Optional[datetime]
        """
        if date is None or isinstance(date, datetime):
            return to_utc(date)
        try:
            dt_date = to_utc(dateutil.parser.isoparse(date))
        except ValueError as e:
            self.log.warn(
                '%r. Date argument "%s" was not in a valid format. Use an ISO 8601 string or a datetime.',
                e,
                date,
            )
            raise
        return dt_date

    def _intrinsic_start_time(self, _db):
        """Return the intrinsic start time for this monitor.

        The intrinsic start time is the time at which this monitor would
        start if it were uninitialized (no timestamp) and no `default_start`
        parameter were supplied. It is `self.DEFAULT_START_TIME`.

        :param _db: Database session object.

        :return: datetime representing a default start time.
        :rtype: datetime
        """
        # We don't use Monitor.timestamp() because that will create
        # the timestamp if it doesn't exist -- we want to see whether
        # or not it exists.
        default_start_time = self.DEFAULT_START_TIME
        initialized = get_one(
            _db,
            Timestamp,
            service=self.service_name,
            service_type=Timestamp.MONITOR_TYPE,
            collection=self.collection,
        )
        if not initialized:
            self.log.info(
                "Initializing %s from date: %s.",
                self.service_name,
                default_start_time.strftime(self.LOG_DATE_FORMAT),
            )
        return default_start_time

    def timestamp(self):
        """Find or create a Timestamp for this Monitor.

        If we are overriding the normal start time with one supplied when
        the this class was instantiated, we do that here. The instance's
        `default_start_time` will have been set to the specified datetime
        and setting`timestamp.finish` to None will cause the default to
        be used.
        """
        timestamp = super(BibliothecaPurchaseMonitor, self).timestamp()
        if self.override_timestamp:
            self.log.info(
                "Overriding timestamp and starting at %s.",
                datetime.strftime(self.default_start_time, self.LOG_DATE_FORMAT),
            )
            timestamp.finish = None
        return timestamp

    def catch_up_from(self, start, cutoff, progress):
        """Ask the Bibliotheca API about new purchases for every
        day between `start` and `cutoff`.

        :param start: The first day to ask about.
        :type start: datetime.datetime
        :param cutoff: The last day to ask about.
        :type cutoff: datetime.datetime
        :param progress: Object used to record progress through the timeline.
        :type progress: core.metadata_layer.TimestampData
        """
        num_records = 0
        # Ask the Bibliotheca API for one day of data at a time.  This
        # ensures that TITLE_ADD events are associated with the day
        # the license was purchased.
        today = utc_now().date()
        achievement_template = "MARC records processed: %s"
        for slice_start, slice_end, is_full_slice in self.slice_timespan(
            start, cutoff, timedelta(days=1)
        ):
            for record in self.purchases(slice_start, slice_end):
                self.process_record(record, slice_start)
                num_records += 1
            if isinstance(slice_end, datetime):
                slice_end_as_date = slice_end.date()
            else:
                slice_end_as_date = slice_end
            if is_full_slice and slice_end_as_date < today:
                # We have finished processing a date in the past.
                # There can never be more licenses purchased for that
                # day. Treat this as a checkpoint.
                #
                # We're playing it safe by using slice_start instead
                # of slice_end here -- slice_end should be fine.
                self._checkpoint(
                    progress, start, slice_start, achievement_template % num_records
                )
            # We're all caught up. The superclass will take care of
            # finalizing the dates, so there's no need to explicitly
            # set a checkpoint.
            progress.achievements = achievement_template % num_records

    def _checkpoint(self, progress, start, finish, achievements):
        """Set the monitor's progress so that if it crashes later on it will
        start from this point, reducing duplicate work.

        This is especially important for this monitor, which usually
        starts several years in the past. TODO: However it might be
        useful to make this a general feature of TimelineMonitor.

        :param progress: Object used to record progress through the timeline.
        :type progress: core.metadata_layer.TimestampData

        :param start: New value for `progress.start`
        :type start: datetime.datetime

        :param finish: New value for `progress.finish`
        :type finish: datetime.datetime

        :param achievements: The monitor's achievements thus far.
        :type achievements: str
        """
        progress.start = start
        progress.finish = finish
        progress.achievements = achievements
        progress.finalize(
            service=self.service_name,
            service_type=Timestamp.MONITOR_TYPE,
            collection=self.collection,
        )
        progress.apply(self._db)
        self._db.commit()

    def purchases(self, start, end):
        """Ask Bibliotheca for a MARC record for each book purchased
        between `start` and `end`.

        :yield: A sequence of pymarc Record objects
        """
        offset = 1  # Smallest allowed offset
        page_size = 50  # Maximum supported size.
        records = None
        while records is None or len(records) >= page_size:
            records = [x for x in self.api.marc_request(start, end, offset, page_size)]
            for record in records:
                yield record
            offset += page_size

    def process_record(self, record, purchase_time):
        """Record the purchase of a new title.

        :param record: Bibliographic information about the new title.
        :type record: pymarc.Record

        :param purchase_time: Put down this time as the time the
           purchase happened.
        :type start_time: datetime.datetime

        :return: A LicensePool representing the new title.
        :rtype: core.model.LicensePool
        """
        # The control number associated with the MARC record is what
        # we call the Bibliotheca ID.
        control_numbers = [x for x in record.fields if x.tag == "001"]
        # These errors should not happen in real usage.
        error = None
        if not control_numbers:
            error = "Ignoring MARC record with no Bibliotheca control number."
        elif len(control_numbers) > 1:
            error = "Ignoring MARC record with multiple Bibliotheca control numbers."
        if error is not None:
            self.log.error(error + " " + record.as_json())
            return

        # At this point we know there is one and only one control
        # number.
        bibliotheca_id = control_numbers[0].value()

        # Find or lookup a LicensePool from the control number.
        license_pool, is_new = LicensePool.for_foreign_id(
            self._db,
            self.api.source,
            Identifier.BIBLIOTHECA_ID,
            bibliotheca_id,
            collection=self.collection,
        )

        if is_new:
            # We've never seen this book before. Immediately acquire
            # bibliographic coverage for it. This will set the
            # DistributionMechanisms and make the book
            # presentation-ready.
            #
            # We have most of the bibliographic information in the
            # MARC record itself, but using the
            # BibliographicCoverageProvider saves code and also gives
            # us up-to-date circulation information.
            coverage_record = self.bibliographic_coverage_provider.ensure_coverage(
                license_pool.identifier, force=True
            )

        # Record the addition of this title to the collection. Do this
        # even if we've heard about the book before, through some
        # other monitor. That's because this is the only Bibliotheca
        # monitor that issues TITLE_ADD events.
        #
        # We know approximately when the license was purchased --
        # potentially a long time ago -- since `start_time` is
        # provided.
        license_pool.collect_analytics_event(
            self.analytics, CirculationEvent.DISTRIBUTOR_TITLE_ADD, purchase_time, 0, 1
        )
        return license_pool


class BibliothecaEventMonitor(BibliothecaTimelineMonitor):

    """Register CirculationEvents for Bibliotheca titles.

    When run, this monitor will look at recent events as a way of keeping
    the local collection up to date.

    Although useful in everyday situations, the events endpoint will
    not always give you all the events:

    * Any given call to the events endpoint will return at most
      100-150 events. If there is a particularly busy 5-minute
      stretch, events will be lost.

    * The Bibliotheca API has, in the past, gone into a state where
      this endpoint returns an empty list of events rather than an
      error message.

    Fortunately, we have the BibliothecaPurchaseMonitor to keep track
    of new license purchases, and the BibliothecaCirculationSweep to
    keep up to date on books we already know about. If the
    BibliothecaEventMonitor stopped working completely, the rest of
    the system would continue to work, but circulation data would
    always be a few hours out of date.

    Thus, running the BibliothecaEventMonitor alongside the other two
    Bibliotheca monitors ensures that circulation data is kept up to date
    in near-real-time with good, but not perfect, consistency.
    """

    SERVICE_NAME = "Bibliotheca Event Monitor"

    def catch_up_from(self, start, cutoff, progress):
        self.log.info(
            "Requesting events between %s and %s",
            start.strftime(self.LOG_DATE_FORMAT),
            cutoff.strftime(self.LOG_DATE_FORMAT),
        )
        events_handled = 0

        # Since we'll never get more than about 100 events from a
        # single API call, slice the timespan into relatively small
        # chunks.
        for slice_start, slice_cutoff, full_slice in self.slice_timespan(
            start, cutoff, timedelta(minutes=5)
        ):
            events = self.api.get_events_between(slice_start, slice_cutoff)
            for event in events:
                self.handle_event(*event)
                events_handled += 1
            self._db.commit()
        progress.achievements = "Events handled: %d." % events_handled

    def handle_event(
        self,
        bibliotheca_id,
        isbn,
        foreign_patron_id,
        start_time,
        end_time,
        internal_event_type,
    ):
        # Find or lookup the LicensePool for this event.
        license_pool, is_new = LicensePool.for_foreign_id(
            self._db,
            self.api.source,
            Identifier.BIBLIOTHECA_ID,
            bibliotheca_id,
            collection=self.collection,
        )

        if is_new:
            # This is a new book. Immediately acquire bibliographic
            # coverage for it.  This will set the
            # DistributionMechanisms and make the book
            # presentation-ready. However, its circulation information
            # might not be up to date until we process some more
            # events.
            #
            # Note that we do not record a TITLE_ADD event for this
            # book; that's the job of the BibliothecaPurchaseMonitor.
            record = self.bibliographic_coverage_provider.ensure_coverage(
                license_pool.identifier, force=True
            )

        bibliotheca_identifier = license_pool.identifier
        isbn, ignore = Identifier.for_foreign_id(self._db, Identifier.ISBN, isbn)

        edition, ignore = Edition.for_foreign_id(
            self._db, self.api.source, Identifier.BIBLIOTHECA_ID, bibliotheca_id
        )

        # The ISBN and the Bibliotheca identifier are exactly equivalent.
        bibliotheca_identifier.equivalent_to(self.api.source, isbn, strength=1)

        # Log the event.
        start = start_time or CirculationEvent.NO_DATE

        # Make sure the effects of the event reported by Bibliotheca
        # are made visible on the LicensePool and turned into
        # analytics events. This is not 100% reliable, but it
        # should be mostly accurate, and the BibliothecaCirculationSweep
        # will periodically correct the errors.
        license_pool.update_availability_from_delta(
            internal_event_type, start_time, 1, self.analytics
        )

        title = edition.title or "[no title]"
        self.log.info(
            "%s %s: %s",
            start_time.strftime(self.LOG_DATE_FORMAT),
            title,
            internal_event_type,
        )
        return start_time


class RunBibliothecaPurchaseMonitorScript(RunCollectionMonitorScript):
    """Adds the ability to specify a particular start date for the
    BibliothecaPurchaseMonitor. This is important because for a given
    collection, the start date needs to be before books
    started being licensed into that collection.
    """

    @classmethod
    def arg_parser(cls):
        parser = super(RunBibliothecaPurchaseMonitorScript, cls).arg_parser()
        parser.add_argument(
            "--default-start",
            metavar="DATETIME",
            default=None,
            type=dateutil.parser.isoparse,
            help="Default start date/time to be used for uninitialized (no timestamp) monitors."
            ' Use ISO 8601 format (e.g., "yyyy-mm-dd", "yyyy-mm-ddThh:mm:ss").'
            " Do not specify a time zone or offset.",
        )
        parser.add_argument(
            "--override-timestamp",
            action="store_true",
            help="Use the specified `--default-start` as the actual"
            " start date, even if a monitor is already initialized.",
        )
        return parser

    @classmethod
    def parse_command_line(cls, _db=None, cmd_args=None, *args, **kwargs):
        parsed = super(RunBibliothecaPurchaseMonitorScript, cls).parse_command_line(
            _db=_db, cmd_args=cmd_args, *args, **kwargs
        )
        if parsed.override_timestamp and not parsed.default_start:
            cls.arg_parser().error(
                '"--override-timestamp" is valid only when "--default-start" is also specified.'
            )
        return parsed


class BibliothecaBibliographicCoverageProvider(BibliographicCoverageProvider):

    """Fill in bibliographic metadata for Bibliotheca records.

    This will occasionally fill in some availability information for a
    single Collection, but we rely on Monitors to keep availability
    information up to date for all Collections.
    """

    SERVICE_NAME = "Bibliotheca Bibliographic Coverage Provider"
    DATA_SOURCE_NAME = DataSource.BIBLIOTHECA
    PROTOCOL = ExternalIntegration.BIBLIOTHECA
    INPUT_IDENTIFIER_TYPES = Identifier.BIBLIOTHECA_ID

    # 25 is the maximum batch size for the Bibliotheca API.
    DEFAULT_BATCH_SIZE = 25

    def __init__(self, collection, api_class=BibliothecaAPI, **kwargs):
        """Constructor.

        :param collection: Provide bibliographic coverage to all
            Bibliotheca books in the given Collection.
        :param api_class: Instantiate this class with the given Collection,
            rather than instantiating BibliothecaAPI.
        :param input_identifiers: Passed in by RunCoverageProviderScript.
            A list of specific identifiers to get coverage for.
        """
        super(BibliothecaBibliographicCoverageProvider, self).__init__(
            collection, **kwargs
        )
        if isinstance(api_class, BibliothecaAPI):
            # This is an already instantiated API object. Use it
            # instead of creating a new one.
            self.api = api_class
        else:
            # A web application should not use this option because it
            # will put a non-scoped session in the mix.
            _db = Session.object_session(collection)
            self.api = api_class(_db, collection)

    def process_item(self, identifier):
        metadata = self.api.bibliographic_lookup(identifier)
        if not metadata:
            return self.failure(identifier, "Bibliotheca bibliographic lookup failed.")
        [metadata] = metadata
        return self.set_metadata(identifier, metadata)
