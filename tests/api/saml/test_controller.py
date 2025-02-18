import json
from unittest.mock import MagicMock, PropertyMock, create_autospec
from urllib.parse import parse_qs, urlencode, urlsplit

from flask import request
from parameterized import parameterized

from api.authenticator import Authenticator, PatronData
from api.saml.auth import SAML_INCORRECT_RESPONSE, SAMLAuthenticationManager
from api.saml.controller import (
    SAML_INVALID_REQUEST,
    SAML_INVALID_RESPONSE,
    SAMLController,
)
from api.saml.metadata.model import (
    SAMLIdentityProviderMetadata,
    SAMLNameIDFormat,
    SAMLOrganization,
    SAMLService,
    SAMLServiceProviderMetadata,
    SAMLUIInfo,
)
from api.saml.provider import SAML_INVALID_SUBJECT, SAMLWebSSOAuthenticationProvider
from core.model import Credential, Library
from core.util.problem_detail import ProblemDetail
from tests.api.saml import fixtures
from tests.api.saml.controller_test import ControllerTest

SERVICE_PROVIDER = SAMLServiceProviderMetadata(
    fixtures.SP_ENTITY_ID,
    SAMLUIInfo(),
    SAMLOrganization(),
    SAMLNameIDFormat.UNSPECIFIED.value,
    SAMLService(fixtures.SP_ACS_URL, fixtures.SP_ACS_BINDING),
)

IDENTITY_PROVIDERS = [
    SAMLIdentityProviderMetadata(
        fixtures.IDP_1_ENTITY_ID,
        SAMLUIInfo(),
        SAMLOrganization(),
        SAMLNameIDFormat.UNSPECIFIED.value,
        SAMLService(fixtures.IDP_1_SSO_URL, fixtures.IDP_1_SSO_BINDING),
        signing_certificates=[fixtures.SIGNING_CERTIFICATE],
    ),
    SAMLIdentityProviderMetadata(
        fixtures.IDP_2_ENTITY_ID,
        SAMLUIInfo(),
        SAMLOrganization(),
        SAMLNameIDFormat.UNSPECIFIED.value,
        SAMLService(fixtures.IDP_2_SSO_URL, fixtures.IDP_2_SSO_BINDING),
    ),
]


def create_patron_data_mock():
    patron_data_mock = create_autospec(spec=PatronData)
    type(patron_data_mock).to_response_parameters = PropertyMock(return_value="")

    return patron_data_mock


class TestSAMLController(ControllerTest):
    @parameterized.expand(
        [
            (
                "with_missing_provider_name",
                None,
                None,
                None,
                SAML_INVALID_REQUEST.detailed(
                    "Required parameter {0} is missing".format(
                        SAMLController.PROVIDER_NAME
                    )
                ),
                None,
            ),
            (
                "with_missing_idp_entity_id",
                SAMLWebSSOAuthenticationProvider.NAME,
                None,
                None,
                SAML_INVALID_REQUEST.detailed(
                    "Required parameter {0} is missing".format(
                        SAMLController.IDP_ENTITY_ID
                    )
                ),
                None,
            ),
            (
                "with_missing_redirect_uri",
                SAMLWebSSOAuthenticationProvider.NAME,
                IDENTITY_PROVIDERS[0].entity_id,
                None,
                SAML_INVALID_REQUEST.detailed(
                    "Required parameter {0} is missing".format(
                        SAMLController.REDIRECT_URI
                    )
                ),
                "http://localhost?"
                + urlencode(
                    {
                        SAMLController.LIBRARY_SHORT_NAME: "default",
                        SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                        SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[0].entity_id,
                    }
                ),
            ),
            (
                "with_all_parameters_set",
                SAMLWebSSOAuthenticationProvider.NAME,
                IDENTITY_PROVIDERS[0].entity_id,
                "http://localhost",
                None,
                "http://localhost?"
                + urlencode(
                    {
                        SAMLController.LIBRARY_SHORT_NAME: "default",
                        SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                        SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[0].entity_id,
                    }
                ),
            ),
            (
                "with_all_parameters_set_and_fragment",
                SAMLWebSSOAuthenticationProvider.NAME,
                IDENTITY_PROVIDERS[0].entity_id,
                "http://localhost#fragment",
                None,
                "http://localhost?"
                + urlencode(
                    {
                        SAMLController.LIBRARY_SHORT_NAME: "default",
                        SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                        SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[0].entity_id,
                    }
                )
                + "#fragment",
            ),
            (
                "with_all_parameters_set_and_redirect_uri_containing_other_parameters",
                SAMLWebSSOAuthenticationProvider.NAME,
                IDENTITY_PROVIDERS[0].entity_id,
                "http://localhost?patron_info=%7B%7D&access_token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                None,
                "http://localhost?"
                + urlencode(
                    {
                        SAMLController.LIBRARY_SHORT_NAME: "default",
                        SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                        SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[0].entity_id,
                        "patron_info": "{}",
                        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                    }
                ),
            ),
        ]
    )
    def test_saml_authentication_redirect(
        self,
        _,
        provider_name,
        idp_entity_id,
        redirect_uri,
        expected_problem,
        expected_relay_state,
    ):
        """Make sure that SAMLController.saml_authentication_redirect creates a correct RelayState or
        returns a correct ProblemDetail object in the case of any error.

        :param provider_name: Name of the authentication provider which should be passed as a request parameter
        :type provider_name: str

        :param idp_entity_id: Identity Provider's ID which should be passed as a request parameter
        :type idp_entity_id: str

        :param expected_problem: (Optional) Expected ProblemDetail object describing the error occurred (if any)
        :type expected_problem: Optional[ProblemDetail]

        :param expected_relay_state: (Optional) String containing the expected RelayState value
        :type expected_relay_state: Optional[str]
        """
        # Arrange
        expected_authentication_redirect_uri = "https://idp.circulationmanager.org"
        authentication_manager = create_autospec(spec=SAMLAuthenticationManager)
        authentication_manager.start_authentication = MagicMock(
            return_value=expected_authentication_redirect_uri
        )
        provider = create_autospec(spec=SAMLWebSSOAuthenticationProvider)
        type(provider).NAME = PropertyMock(
            return_value=SAMLWebSSOAuthenticationProvider.NAME
        )
        provider.get_authentication_manager = MagicMock(
            return_value=authentication_manager
        )
        provider.library = MagicMock(return_value=self._default_library)
        authenticator = Authenticator(self._db, self._db.query(Library))

        authenticator.library_authenticators["default"].register_saml_provider(provider)

        controller = SAMLController(self.app.manager, authenticator)
        params = {}

        if provider_name:
            params[SAMLController.PROVIDER_NAME] = provider_name
        if idp_entity_id:
            params[SAMLController.IDP_ENTITY_ID] = idp_entity_id
        if redirect_uri:
            params[SAMLController.REDIRECT_URI] = redirect_uri

        query = urlencode(params)

        with self.app.test_request_context(
            "http://circulationmanager.org/saml_authenticate?" + query
        ):
            request.library = self._default_library

            # Act
            result = controller.saml_authentication_redirect(request.args, self._db)

            # Assert
            if expected_problem:
                assert isinstance(result, ProblemDetail)
                assert result.response == expected_problem.response
            else:
                assert 302 == result.status_code
                assert expected_authentication_redirect_uri == result.headers.get(
                    "Location"
                )

                authentication_manager.start_authentication.assert_called_once_with(
                    self._db, idp_entity_id, expected_relay_state
                )

    @parameterized.expand(
        [
            (
                "with_missing_relay_state",
                None,
                None,
                None,
                None,
                None,
                SAML_INVALID_RESPONSE.detailed(
                    "Required parameter {0} is missing from the response body".format(
                        SAMLController.RELAY_STATE
                    )
                ),
            ),
            (
                "with_incorrect_relay_state",
                {SAMLController.RELAY_STATE: "<>"},
                None,
                None,
                None,
                None,
                SAML_INVALID_RESPONSE.detailed(
                    "Required parameter {0} is missing from RelayState".format(
                        SAMLController.LIBRARY_SHORT_NAME
                    )
                ),
            ),
            (
                "with_missing_provider_name",
                {
                    SAMLController.RELAY_STATE: "http://localhost?"
                    + urlencode({SAMLController.LIBRARY_SHORT_NAME: "default"})
                },
                None,
                None,
                None,
                None,
                SAML_INVALID_RESPONSE.detailed(
                    "Required parameter {0} is missing from RelayState".format(
                        SAMLController.PROVIDER_NAME
                    )
                ),
            ),
            (
                "with_missing_idp_entity_id",
                {
                    SAMLController.RELAY_STATE: "http://localhost?"
                    + urlencode(
                        {
                            SAMLController.LIBRARY_SHORT_NAME: "default",
                            SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                        }
                    )
                },
                None,
                None,
                None,
                None,
                SAML_INVALID_RESPONSE.detailed(
                    "Required parameter {0} is missing from RelayState".format(
                        SAMLController.IDP_ENTITY_ID
                    )
                ),
            ),
            (
                "when_finish_authentication_fails",
                {
                    SAMLController.RELAY_STATE: "http://localhost?"
                    + urlencode(
                        {
                            SAMLController.LIBRARY_SHORT_NAME: "default",
                            SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                            SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[
                                0
                            ].entity_id,
                        }
                    )
                },
                SAML_INCORRECT_RESPONSE.detailed("Authentication failed"),
                None,
                None,
                None,
                None,
            ),
            (
                "when_saml_callback_fails",
                {
                    SAMLController.RELAY_STATE: "http://localhost?"
                    + urlencode(
                        {
                            SAMLController.LIBRARY_SHORT_NAME: "default",
                            SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                            SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[
                                0
                            ].entity_id,
                        }
                    )
                },
                None,
                SAML_INVALID_SUBJECT.detailed("Authentication failed"),
                None,
                None,
                None,
            ),
            (
                "when_saml_callback_returns_correct_patron",
                {
                    SAMLController.RELAY_STATE: "http://localhost?"
                    + urlencode(
                        {
                            SAMLController.LIBRARY_SHORT_NAME: "default",
                            SAMLController.PROVIDER_NAME: SAMLWebSSOAuthenticationProvider.NAME,
                            SAMLController.IDP_ENTITY_ID: IDENTITY_PROVIDERS[
                                0
                            ].entity_id,
                        }
                    )
                },
                None,
                (create_autospec(spec=Credential), object(), create_patron_data_mock()),
                "ABCDEFG",
                "http://localhost?access_token=ABCDEFG&patron_info=%22%22",
                None,
            ),
        ]
    )
    def test_saml_authentication_callback(
        self,
        _,
        data,
        finish_authentication_result,
        saml_callback_result,
        bearer_token,
        expected_authentication_redirect_uri,
        expected_problem,
    ):
        # Arrange
        authentication_manager = create_autospec(spec=SAMLAuthenticationManager)
        authentication_manager.finish_authentication = MagicMock(
            return_value=finish_authentication_result
        )
        provider = create_autospec(spec=SAMLWebSSOAuthenticationProvider)
        type(provider).NAME = PropertyMock(
            return_value=SAMLWebSSOAuthenticationProvider.NAME
        )
        provider.get_authentication_manager = MagicMock(
            return_value=authentication_manager
        )
        provider.library = MagicMock(return_value=self._default_library)
        provider.saml_callback = MagicMock(return_value=saml_callback_result)
        authenticator = Authenticator(self._db, libraries=self._db.query(Library))

        authenticator.library_authenticators["default"].register_saml_provider(provider)
        authenticator.bearer_token_signing_secret = "test"
        authenticator.library_authenticators[
            "default"
        ].bearer_token_signing_secret = "test"
        authenticator.create_bearer_token = MagicMock(return_value=bearer_token)

        controller = SAMLController(self.app.manager, authenticator)

        with self.app.test_request_context(
            "http://circulationmanager.org/saml_callback", data=data
        ):
            # Act
            result = controller.saml_authentication_callback(request, self._db)

            # Assert
            if isinstance(finish_authentication_result, ProblemDetail) or isinstance(
                saml_callback_result, ProblemDetail
            ):
                assert result.status_code == 302

                query_items = parse_qs(urlsplit(result.location).query)

                assert SAMLController.ERROR in query_items

                error = query_items[SAMLController.ERROR][0]
                error = json.loads(error)

                problem = (
                    finish_authentication_result
                    if finish_authentication_result
                    else saml_callback_result
                )
                assert error["type"] == problem.uri
                assert error["status"] == problem.status_code
                assert error["title"] == problem.title
                assert error["detail"] == problem.detail
            elif expected_problem:
                assert isinstance(result, ProblemDetail)
                assert result.response == expected_problem.response
            else:
                assert result.status_code == 302
                assert (
                    result.headers.get("Location")
                    == expected_authentication_redirect_uri
                )

                authentication_manager.finish_authentication.assert_called_once_with(
                    self._db, IDENTITY_PROVIDERS[0].entity_id
                )
                provider.saml_callback.assert_called_once_with(
                    self._db, finish_authentication_result
                )
