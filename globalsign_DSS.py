import asyncio
import hashlib
import requests
import os

from asn1crypto import x509, pem
from pyhanko.sign import signers
from pyhanko.sign.fields import SigFieldSpec
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko_certvalidator.registry import SimpleCertificateStore
from pyhanko.pdf_utils.reader import PdfFileReader
from dotenv import load_dotenv
# from pyhanko.sign.appearance import TextStampStyle


DSS_BASE_URL = "https://emea.api.dss.globalsign.com:8443/v2"

load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# mTLS client cert + key
MTLS_CERT = ("yourmTLS.pem", "yourprivatekey.key")

INPUT_PDF = "yourpdf"
name, ext = os.path.splitext(INPUT_PDF)
OUTPUT_PDF = f"{name}-signed{ext}"


def load_cert_from_pem(pem_text: str):
    if "\\n" in pem_text:
        pem_text = pem_text.replace("\\n", "\n")

    _, _, der_bytes = pem.unarmor(pem_text.encode("utf-8"))
    return x509.Certificate.load(der_bytes)


class GlobalSignDSSClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.cert = MTLS_CERT
        self.access_token = None
        self.identity = None

    def login(self):
        response = self.session.post(
            f"{DSS_BASE_URL}/login",
            json={
                "api_key": API_KEY,
                "api_secret": API_SECRET,
            },
            headers={
                "Content-Type": "application/json;charset=utf-8",
                "Accept": "application/json",
            },
            timeout=60,
        )

        response.raise_for_status()
        data = response.json()

        self.access_token = data["access_token"]
        return self.access_token

    def auth_headers(self):
        if not self.access_token:
            self.login()

        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json",
        }

    def retrieve_identity(self):
        response = self.session.post(
            f"{DSS_BASE_URL}/identity",
            json={},  # REQUIRED (prevents 411)
            headers={
                **self.auth_headers(),
                "Content-Type": "application/json;charset=utf-8",
                "Accept": "application/json",
            },
            timeout=60,
        )

        response.raise_for_status()
        data = response.json()

        self.identity = data
        return data

    def get_identity_id(self):
        if not self.identity:
            self.retrieve_identity()

        return self.identity["id"]

    def get_signing_cert_pem(self):
        if not self.identity:
            self.retrieve_identity()

        cert = self.identity.get("signing_cert") or self.identity.get("signing_certificate")

        if not cert:
            raise ValueError(f"No signing cert found in: {self.identity}")

        return cert.replace("\\n", "\n")

    def sign_digest(self, digest_bytes: bytes) -> bytes:
        identity_id = self.get_identity_id()
        digest_hex = digest_bytes.hex()

        response = self.session.get(
            f"{DSS_BASE_URL}/identity/{identity_id}/sign/{digest_hex}",
            headers=self.auth_headers(),
            timeout=60,
        )

        response.raise_for_status()
        data = response.json()

        signature_hex = data.get("signature")
        if not signature_hex:
            raise ValueError(f"No signature in response: {data}")

        return bytes.fromhex(signature_hex)


class GlobalSignPDFSigner(signers.Signer):
    def __init__(self, dss_client: GlobalSignDSSClient):
        self.dss_client = dss_client

        signing_cert_pem = dss_client.get_signing_cert_pem()
        signing_cert = load_cert_from_pem(signing_cert_pem)

        cert_registry = SimpleCertificateStore()

        super().__init__(
            signing_cert=signing_cert,
            cert_registry=cert_registry,
        )

    async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False) -> bytes:
        if dry_run:
            return bytes(256)  # RSA-2048

        digest = hashlib.new(digest_algorithm, data).digest()
        return self.dss_client.sign_digest(digest)


async def sign_pdf():
    dss_client = GlobalSignDSSClient()

    print("Logging in...")
    dss_client.login()

    print("Retrieving identity...")
    identity = dss_client.retrieve_identity()
    print("Identity ID:", identity["id"])

    signer = GlobalSignPDFSigner(dss_client)

    with open(INPUT_PDF, "rb") as inf:

        reader = PdfFileReader(inf)
        num_pages = reader.root['/Pages']['/Count']

        page_index = 0 if num_pages == 1 else -1

        writer = IncrementalPdfFileWriter(inf)

        # stamp_style = TextStampStyle(
        #     stamp_text=(
        #         "Digitally signed by: %(signer)s\n"
        #         "Date: %(ts)s\n"
        #         "Reason: %(reason)s\n"
        #         "Location: %(location)s"
        #     )
        # )

        signed_pdf = await signers.async_sign_pdf(
            writer,
            signers.PdfSignatureMetadata(
                field_name="Signature1",
                md_algorithm="sha256",
                reason="Approved contract",
                location="Makati, PH",
                name="Christ ND",
            ),
            signer=signer,
            new_field_spec=SigFieldSpec(
                sig_field_name="Signature1",
                box=(300, 50, 550, 120),
                on_page=page_index,
            ),
            # appearance=stamp_style,
        )

    with open(OUTPUT_PDF, "wb") as outf:
        outf.write(signed_pdf.getbuffer())

    print(f"Signed PDF saved as: {OUTPUT_PDF}")


if __name__ == "__main__":
    asyncio.run(sign_pdf())