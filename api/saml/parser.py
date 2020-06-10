import logging

from defusedxml.lxml import fromstring
from flask_babel import lazy_gettext as _
from lxml.etree import XMLSyntaxError
from onelogin.saml2.constants import OneLogin_Saml2_Constants
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from api.saml.exceptions import SAMLError
from api.saml.metadata import IdentityProviderMetadata, LocalizableMetadataItem, UIInfo, ServiceProviderMetadata, \
    Binding, Service, NameIDFormat


class SAMLMetadataParsingError(SAMLError):
    """Raised in the case of any errors occurred during parsing of SAML metadata"""


class SAMLMetadataParser(object):
    """Parses SAML metadata"""

    def __init__(self):
        """Initializes a new instance of MetadataParser class"""
        self._logger = logging.getLogger(__name__)

        # Add missing namespaces to be able to parse mdui:UIInfoType
        OneLogin_Saml2_Constants.NS_PREFIX_MDUI = 'mdui'
        OneLogin_Saml2_Constants.NS_MDUI = 'urn:oasis:names:tc:SAML:metadata:ui'
        OneLogin_Saml2_Constants.NSMAP[OneLogin_Saml2_Constants.NS_PREFIX_MDUI] = OneLogin_Saml2_Constants.NS_MDUI

        OneLogin_Saml2_Constants.NS_PREFIX_ALG = 'alg'
        OneLogin_Saml2_Constants.NS_ALG = 'urn:oasis:names:tc:SAML:metadata:algsupport'
        OneLogin_Saml2_Constants.NSMAP[OneLogin_Saml2_Constants.NS_PREFIX_ALG] = OneLogin_Saml2_Constants.NS_ALG

    def _convert_xml_string_to_dom(self, xml_metadata):
        """Converts an XML string containing SAML metadata into XML DOM

        :param xml_metadata: XML string containing SAML metadata
        :type xml_metadata: string

        :return: XML DOM tree containing SAML metadata
        :rtype: defusedxml.lxml.RestrictedElement

        :raise: MetadataParsingError
        """
        self._logger.debug('Started converting XML string containing SAML metadata into XML DOM')

        try:
            metadata_dom = fromstring(xml_metadata, forbid_dtd=True)
        except (ValueError, XMLSyntaxError,) as exception:
            self._logger.exception(
                'An unhandled exception occurred during converting XML string containing SAML metadata into XML DOM')

            raise SAMLMetadataParsingError(inner_exception=exception)

        self._logger.debug('Finished converting XML string containing SAML metadata into XML DOM')

        return metadata_dom

    def _parse_certificates(self, certificate_nodes):
        """Parses XML nodes containing X.509 certificates into a list of strings

        :param certificate_nodes: List of XML nodes containing X.509 certificates
        :type certificate_nodes: List[defusedxml.lxml.RestrictedElement]

        :return: List of string containing X.509 certificates
        :rtype: List[string]

        :raise: MetadataParsingError
        """
        certificates = []

        try:
            for certificate_node in certificate_nodes:
                certificates.append(''.join(OneLogin_Saml2_Utils.element_text(certificate_node).split()))
        except XMLSyntaxError as exception:
            raise SAMLMetadataParsingError(inner_exception=exception)

        return certificates

    def _parse_providers(self, entity_descriptor_node, provider_nodes, parse_function):
        """Parses a list of IDPSSODescriptor/SPSSODescriptor nodes and translates them
        into IdentityProviderMetadata/ServiceProviderMetadata object

        :param entity_descriptor_node: Parent EntityDescriptor node
        :type entity_descriptor_node: defusedxml.lxml.RestrictedElement

        :param provider_nodes: List of IDPSSODescriptor/SPSSODescriptor nodes
        :type provider_nodes: List[defusedxml.lxml.RestrictedElement]

        :param parse_function: Function used to parse body of IDPSSODescriptor/SPSSODescriptor nodes
        and return corresponding IdentityProviderMetadata/ServiceProviderMetadata objects
        :type parse_function: Callable[[defusedxml.lxml.RestrictedElement, string, UIInfo], ProviderMetadata]

        :return: List of IdentityProviderMetadata/ServiceProviderMetadata objects containing SAML metadata from the XML
        :rtype: List[ProviderMetadata]

        :raise: MetadataParsingError
        """
        providers = []

        for provider_node in provider_nodes:
            entity_id = entity_descriptor_node.get('entityID', None)
            ui_info = self._parse_ui_info(provider_node)
            provider = parse_function(provider_node, entity_id, ui_info)

            providers.append(provider)

        return providers

    def _parse_ui_info_item(self, provider_descriptor_node, xpath, required=False):
        """Parses IDPSSODescriptor/SPSSODescriptor's mdui:UIInfo child elements (for example, mdui:DisplayName)

        :param provider_descriptor_node: Parent IDPSSODescriptor/SPSSODescriptor XML node
        :type provider_descriptor_node: defusedxml.lxml.RestrictedElement

        :param xpath: XPath expression for a particular mdui:UIInfo child element (for example, mdui:DisplayName)
        :type xpath: string

        :param required: Boolean value indicating whether particular mdui:UIInfo child element is required or not
        :type required: bool

        :return: List of mdui:UIInfo child elements
        :rtype: List[LocalizableMetadataItem]

        :raise: MetadataParsingError
        """
        ui_info_item_nodes = OneLogin_Saml2_Utils.query(provider_descriptor_node, xpath)

        if not ui_info_item_nodes and required:
            last_slash_index = xpath.rfind('/')
            ui_info_item_name = xpath[last_slash_index + 1:]

            raise SAMLMetadataParsingError(_('{0} tag is missing'.format(ui_info_item_name)))

        ui_info_items = None

        if ui_info_item_nodes:
            ui_info_items = []

            for ui_info_item_node in ui_info_item_nodes:
                ui_info_item_text = ui_info_item_node.text
                ui_info_item_language = ui_info_item_node.get('{http://www.w3.org/XML/1998/namespace}lang', None)
                ui_info_item = LocalizableMetadataItem(ui_info_item_text, ui_info_item_language)

                ui_info_items.append(ui_info_item)

        return ui_info_items

    def _parse_ui_info(self, provider_node):
        """Parses IDPSSODescriptor/SPSSODescriptor's mdui:UIInfo and translates it into UIInfo object

        :param provider_node: Parent IDPSSODescriptor/SPSSODescriptor node
        :type provider_node: defusedxml.lxml.RestrictedElement

        :return: UIInfo object
        :rtype: UIInfo

        :raise: MetadataParsingError
        """
        display_names = self._parse_ui_info_item(
            provider_node, './md:Extensions/mdui:UIInfo/mdui:DisplayName')
        descriptions = self._parse_ui_info_item(
            provider_node, './md:Extensions/mdui:UIInfo/mdui:Description')
        information_urls = self._parse_ui_info_item(
            provider_node, './md:Extensions/mdui:UIInfo/mdui:InformationURL')
        privacy_statement_urls = self._parse_ui_info_item(
            provider_node, './md:Extensions/mdui:UIInfo/mdui:PrivacyStatementURL')
        logos = self._parse_ui_info_item(
            provider_node, './md:Extensions/mdui:UIInfo/mdui:Logo')

        ui_info = UIInfo(
            display_names,
            descriptions,
            information_urls,
            privacy_statement_urls,
            logos
        )

        return ui_info

    def _parse_idp_metadata(
            self,
            provider_node,
            entity_id,
            ui_info,
            required_sso_binding=Binding.HTTP_REDIRECT,
            required_slo_binding=Binding.HTTP_REDIRECT):
        """Parses IDPSSODescriptor node and translates it into an IdentityProviderMetadata object

        :param provider_node: IDPSSODescriptor node containing IdP metadata
        :param provider_node: defusedxml.lxml.RestrictedElement

        :param entity_id: String containing IdP's entityID
        :type entity_id: string

        :param ui_info: UIInfo object containing IdP's description
        :type ui_info: UIInfo

        :param required_sso_binding: Required binding for Single Sign-On profile (HTTP-Redirect by default)
        :type required_sso_binding: Binding

        :param required_slo_binding: Required binding for Single Sing-Out profile (HTTP-Redirect by default)
        :type required_slo_binding: Binding

        :return: IdentityProviderMetadata containing IdP metadata
        :rtype: IdentityProviderMetadata

        :raise: MetadataParsingError
        """
        want_authn_requests_signed = provider_node.get('WantAuthnRequestsSigned', False)

        name_id_format = NameIDFormat.UNSPECIFIED.value
        name_id_format_nodes = OneLogin_Saml2_Utils.query(provider_node, './ md:NameIDFormat')
        if len(name_id_format_nodes) > 0:
            name_id_format = OneLogin_Saml2_Utils.element_text(name_id_format_nodes[0])

        sso_service = None
        sso_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            "./md:SingleSignOnService[@Binding='%s']" % required_sso_binding.value
        )
        if len(sso_nodes) > 0:
            sso_url = sso_nodes[0].get('Location', None)
            sso_service = Service(sso_url, required_sso_binding)
        else:
            raise SAMLMetadataParsingError(
                _('Missing {0} SingleSignOnService service declaration'.format(required_sso_binding.value)))

        slo_service = None
        slo_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            "./md:SingleLogoutService[@Binding='%s']" % required_slo_binding.value
        )
        if len(slo_nodes) > 0:
            slo_url = slo_nodes[0].get('Location', None)
            slo_service = Service(slo_url, required_slo_binding)

        signing_certificate_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            './md:KeyDescriptor[not(contains(@use, "encryption"))]/ds:KeyInfo/ds:X509Data/ds:X509Certificate')
        signing_certificates = self._parse_certificates(signing_certificate_nodes)

        encryption_certificate_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            './md:KeyDescriptor[not(contains(@use, "signing"))]/ds:KeyInfo/ds:X509Data/ds:X509Certificate')
        encryption_certificates = self._parse_certificates(encryption_certificate_nodes)

        idp = IdentityProviderMetadata(
            entity_id,
            ui_info,
            name_id_format,
            sso_service,
            slo_service,
            want_authn_requests_signed,
            signing_certificates,
            encryption_certificates)

        return idp

    def _parse_sp_metadata(
            self,
            provider_node,
            entity_id,
            ui_info,
            required_acs_binding=Binding.HTTP_POST):
        """Parses SPSSODescriptor node and translates it into a ServiceProvider object

        :param provider_node: SPSSODescriptor node containing SP metadata
        :param provider_node: defusedxml.lxml.RestrictedElement

        :param entity_id: String containing IdP's entityID
        :type entity_id: string

        :param ui_info: UIInfo object containing IdP's description
        :type ui_info: UIInfo

        :param required_acs_binding: Required binding for Assertion Consumer Service (HTTP-Redirect by default)
        :type required_acs_binding: Binding

        :return: ServiceProvider containing SP metadata
        :rtype: ServiceProvider

        :raise: MetadataParsingError
        """
        authn_requests_signed = provider_node.get('AuthnRequestsSigned', False)
        want_assertions_signed = provider_node.get('WantAssertionsSigned', False)

        name_id_format = NameIDFormat.UNSPECIFIED.value
        name_id_format_nodes = OneLogin_Saml2_Utils.query(provider_node, './ md:NameIDFormat')
        if len(name_id_format_nodes) > 0:
            name_id_format = OneLogin_Saml2_Utils.element_text(name_id_format_nodes[0])

        acs_service = None
        acs_service_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            "./md:AssertionConsumerService[@Binding='%s']" % required_acs_binding.value
        )
        if len(acs_service_nodes) > 0:
            acs_url = acs_service_nodes[0].get('Location', None)
            acs_service = Service(acs_url, required_acs_binding)
        else:
            raise SAMLMetadataParsingError(_('Missing {0} AssertionConsumerService'.format(required_acs_binding.value)))

        certificate_nodes = OneLogin_Saml2_Utils.query(
            provider_node,
            './md:KeyDescriptor/ds:KeyInfo/ds:X509Data/ds:X509Certificate')
        certificates = self._parse_certificates(certificate_nodes)

        sp = ServiceProviderMetadata(
            entity_id,
            ui_info,
            name_id_format,
            acs_service,
            authn_requests_signed,
            want_assertions_signed,
            certificates)

        return sp

    def parse(self, xml_metadata):
        """Parses an XML string containing SAML metadata and translates it into a list of
        IdentityProviderMetadata/ServiceProviderMetadata objects

        :param xml_metadata: XML string containing SAML metadata
        :type xml_metadata: string

        :return: List of IdentityProviderMetadata/ServiceProviderMetadata objects
        :rtype: List[ProviderMetadata]

        :raise: MetadataParsingError
        """
        self._logger.info('Started parsing an XML string containing SAML metadata')

        metadata_dom = self._convert_xml_string_to_dom(xml_metadata)
        providers = []

        try:
            entity_descriptor_nodes = OneLogin_Saml2_Utils.query(metadata_dom, '//md:EntityDescriptor')

            for entity_descriptor_node in entity_descriptor_nodes:
                idp_descriptor_nodes = OneLogin_Saml2_Utils.query(entity_descriptor_node, './md:IDPSSODescriptor')
                idps = self._parse_providers(
                    entity_descriptor_node, idp_descriptor_nodes, self._parse_idp_metadata)
                providers += idps

                sp_descriptor_nodes = OneLogin_Saml2_Utils.query(entity_descriptor_node, './md:SPSSODescriptor')
                sps = self._parse_providers(
                    entity_descriptor_node, sp_descriptor_nodes, self._parse_sp_metadata)
                providers += sps
        except XMLSyntaxError as exception:
            self._logger.exception('An unexpected error occurred during parsing an XML string containing SAML metadata')

            raise SAMLMetadataParsingError(inner_exception=exception)

        self._logger.info('Finished parsing an XML string containing SAML metadata')

        return providers
