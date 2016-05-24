from nose.tools import set_trace
import requests
import urlparse
from problem_detail import ProblemDetail as pd

INTEGRATION_ERROR = pd(
      "http://librarysimplified.org/terms/problem/remote-integration-failed",
      502,
      "Third-party service failed.",
      "A third-party service has failed.",
)

class RemoteIntegrationException(Exception):

    """An exception that happens when communicating with a third-party
    service.
    """
    title = "Network failure contacting external service"
    detail = "The server experienced a network error while accessing %s."
    internal_message = "Network error accessing %s: %s"

    def __init__(self, url, message, debug_message=None):
        super(RemoteIntegrationException, self).__init__(message)
        self.url = url
        self.hostname = urlparse.urlparse(url).netloc
        self.debug_message = debug_message

    def __str__(self):
        return self.internal_message % (self.url, self.message)

    def document_detail(self, debug=True):
        if debug:
            return self.detail % self.url
        return self.detail % self.hostname

    def document_debug_message(self, debug=True):
        if debug:
            return self.detail % self.url
        return None

    def as_problem_detail_document(self, debug):
        return INTEGRATION_ERROR.detailed(
            detail=self.document_detail(debug), title=self.title, 
            debug_message=self.document_debug_message(debug)
        )

class BadResponseException(RemoteIntegrationException):
    """The request seemingly went okay, but we got a bad response."""
    title = "Bad response"
    detail = "The server made a request to %s, and got an unexpected or invalid response."
    internal_message = "Bad response from %s: %s"

    BAD_STATUS_CODE_MESSAGE = "Got status code %s from external server, cannot continue."

    def document_debug_message(self, debug=True):
        if debug:
            msg = self.message
            if self.debug_message:
                msg += "\n\n" + self.debug_message
            return msg
        return None

    @classmethod
    def from_response(cls, url, message, response):
        """Helper method to turn a `requests` Response object into
        a BadResponseException.
        """
        if isinstance(response, tuple):
            # The response has been unrolled into a (status_code,
            # headers, body) 3-tuple.
            status_code, headers, content = response
        else:
            status_code = response.status_code
            content = response.content
        return BadResponseException(
            url, message, 
            debug_message="Status code: %s\nContent: %s" % (
                status_code,
                content,
            )
        )

    @classmethod
    def bad_status_code(cls, url, response):
        """The response is bad because the status code is wrong."""
        message = cls.BAD_STATUS_CODE_MESSAGE % response.status_code
        return cls.from_response(
            url,
            message,
            response,
        )


class RequestNetworkException(RemoteIntegrationException,
                              requests.exceptions.RequestException):
    """An exception from the requests module that can be represented as
    a problem detail document.
    """
    pass

class RequestTimedOut(RequestNetworkException, requests.exceptions.Timeout):
    """A timeout exception that can be represented as a problem
    detail document.
    """

    title = "Timeout"
    detail = "The server made a request to %s, and that request timed out."
    internal_message = "Timeout accessing %s: %s"


class HTTP(object):
    """A helper for the `requests` module."""

    @classmethod
    def get_with_timeout(cls, url, *args, **kwargs):
        """Make a GET request with timeout handling."""
        return cls.request_with_timeout("GET", url, *args, **kwargs)

    @classmethod
    def post_with_timeout(cls, url, payload, *args, **kwargs):
        """Make a POST request with timeout handling."""
        kwargs['data'] = payload
        return cls.request_with_timeout("POST", url, *args, **kwargs)

    @classmethod
    def request_with_timeout(cls, http_method, url, *args, **kwargs):
        """Call requests.request and turn a timeout into a RequestTimedOut
        exception.
        """
        return cls._request_with_timeout(
            url, requests.request, http_method, url, *args, **kwargs
        )

    @classmethod
    def _request_with_timeout(cls, url, m, *args, **kwargs):
        """Call some kind of method and turn a timeout into a RequestTimedOut
        exception.

        The core of `request_with_timeout` made easy to test.
        """
        allowed_response_codes = kwargs.get('allowed_response_codes')
        if 'allowed_response_codes' in kwargs:
            del kwargs['allowed_response_codes']
        disallowed_response_codes = kwargs.get('disallowed_response_codes')
        if 'disallowed_response_codes' in kwargs:
            del kwargs['disallowed_response_codes']

        if not 'timeout' in kwargs:
            kwargs['timeout'] = 20
        try:
            response = m(*args, **kwargs)
        except requests.exceptions.Timeout, e:
            # Wrap the requests-specific Timeout exception 
            # in a generic RequestTimedOut exception.
            raise RequestTimedOut(url, e.message)
        except requests.exceptions.RequestException, e:
            # Wrap all other requests-specific exceptions in
            # a generic RequestNetworkException.
            raise RequestNetworkException(url, e.message)

        return cls._process_response(
            url, response, allowed_response_codes, disallowed_response_codes
        )

    @classmethod
    def _process_response(cls, url, response, allowed_response_codes=None,
                          disallowed_response_codes=None):
        """Raise a RequestNetworkException if the response code indicates a
        server-side failure, or behavior so unpredictable that we can't
        continue.
        """
        if allowed_response_codes:
            allowed_response_codes = map(str, allowed_response_codes)
            status_code_not_in_allowed = "Got status code %%s from external server, but can only continue on: %s." % ", ".join(sorted(allowed_response_codes))
        if disallowed_response_codes:
            disallowed_response_codes = map(str, disallowed_response_codes)
        else:
            disallowed_response_codes = []

        code = response.status_code
        series = "%sxx" % (code / 100)
        code = str(code)

        if allowed_response_codes and (
                code in allowed_response_codes 
                or series in allowed_response_codes
        ):
            # The code or series has been explicitly allowed. Allow
            # the request to be processed.
            return response

        error_message = None
        if (series == '5xx' or code in disallowed_response_codes
            or series in disallowed_response_codes
        ):
            # Unless explicitly allowed, the 5xx series always results in
            # an exception.
            error_message = BadResponseException.BAD_STATUS_CODE_MESSAGE
        elif (allowed_response_codes and not (
                code in allowed_response_codes 
                or series in allowed_response_codes
        )):
            error_message = status_code_not_in_allowed

        if error_message:
            raise BadResponseException(
                url,
                error_message % code, 
                debug_message="Response content: %s" % response.content
            )
        return response

