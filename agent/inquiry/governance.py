"""Deterministic privacy, entity-resolution and evidence rules for research."""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

from .repository import digest


FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",
}
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


def _text(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _host(value):
    raw = _text(value).casefold()
    if "@" in raw and not raw.startswith(("http://", "https://")):
        raw = raw.rsplit("@", 1)[-1]
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlsplit(raw).hostname or "").removeprefix("www.").strip(".")
    return host if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) else ""


def _domain_from_email(value):
    match = EMAIL_PATTERN.search(_text(value))
    if not match:
        return ""
    domain = match.group(1).casefold().strip(".")
    return "" if domain in FREE_EMAIL_DOMAINS else domain


def _normalized(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _text(value, 4000).casefold())


def _digits(value):
    return re.sub(r"\D", "", _text(value, 100))


def redact_external_text(value):
    """Remove direct personal contact data before transfer to a model/vendor."""
    text = _text(value, 20000)
    text = EMAIL_PATTERN.sub(lambda match: f"[email-domain:{match.group(1).casefold()}]", text)
    return PHONE_PATTERN.sub("[phone-redacted]", text)


class OutboundResearchContext:
    """Allowlisted public company identifiers for every external adapter."""

    def __init__(self, seed):
        self.seed = seed

    @classmethod
    def from_case(cls, case):
        rfq = case.get("data", {}).get("rfq", {})
        customer = rfq.get("customer", {}) if isinstance(rfq, dict) else {}
        website_domain = _host(customer.get("website"))
        email_domain = _domain_from_email(customer.get("email"))
        seed = {
            "legal_name": _text(customer.get("company") or customer.get("legal_name")),
            "official_domain": website_domain or email_domain,
            "email_domain": email_domain,
            # RFQ country is the delivery destination, not the customer's
            # registered country. Only an explicit customer field is safe here.
            "country": _text(customer.get("country")),
            "address": _text(customer.get("address")),
            "city": _text(customer.get("city")),
            "phone": _text(customer.get("phone")),
            "registration_number": _text(customer.get("registration_number") or customer.get("registration_id")),
        }
        return cls(seed)

    def public_seed(self):
        return {key: value for key, value in self.seed.items() if value and key != "phone"}

    @staticmethod
    def model_identity(identity):
        allowed = {"legal_name", "official_domain", "email_domain", "country", "address", "city",
                   "registration_number", "company_identity_id", "confidence", "status",
                   "public_profile_verified", "matches", "gaps"}
        return {key: copy.deepcopy(value) for key, value in identity.items() if key in allowed}

    def model_rfq(self, rfq):
        """Keep procurement facts while removing raw extraction and personal contacts."""
        if not isinstance(rfq, dict):
            return {}
        customer = rfq.get("customer", {}) if isinstance(rfq.get("customer"), dict) else {}
        return {
            "customer": {
                "company": self.seed.get("legal_name", ""),
                "website_domain": self.seed.get("official_domain", ""),
            },
            "country": _text(rfq.get("country")),
            "items": copy.deepcopy(rfq.get("items", []))[:100]
            if isinstance(rfq.get("items", []), list) else [],
            "trade_term": copy.deepcopy(rfq.get("trade_term")),
        }

    def query(self, tool):
        allowed = {
            "official_website": ("legal_name", "official_domain", "country"),
            "map_lookup": ("legal_name", "city", "country", "address"),
            "customs_lookup": ("legal_name", "country", "registration_number"),
            "news_search": ("legal_name", "official_domain", "country"),
        }
        values = [self.seed.get(key, "") for key in allowed.get(tool, ())]
        if tool == "official_website" and not (self.seed.get("legal_name") or self.seed.get("official_domain")):
            return ""
        if tool in {"map_lookup", "customs_lookup", "news_search"} and not self.seed.get("legal_name"):
            return ""
        if tool == "customs_lookup":
            query = json.dumps({
                "legal_name": self.seed.get("legal_name", ""),
                "country": self.seed.get("country", ""),
                "registration_number": self.seed.get("registration_number", ""),
            }, ensure_ascii=False, separators=(",", ":"))
        else:
            query = " ".join(value for value in values if value)
        query = EMAIL_PATTERN.sub("", query)
        return _text(query, 500)

    @staticmethod
    def model_messages(messages):
        rows = []
        for message in messages:
            row = copy.deepcopy(message)
            sender_domain = _domain_from_email(row.get("sender")) or _host(row.get("sender"))
            row["sender"] = sender_domain
            row["sender_name"] = ""
            row["subject"] = redact_external_text(row.get("subject"))
            row["text"] = redact_external_text(row.get("text"))
            row.pop("archive", None)
            row.pop("attachments", None)
            rows.append(row)
        return rows

    @staticmethod
    def model_evidence(evidence):
        rows = []
        for item in evidence:
            if item.get("source_type") == "business_history":
                # Old reports can contain stale or unrelated findings and
                # customer contact data. They remain in the audit store, not
                # in a new model request.
                continue
            row = copy.deepcopy(item)
            if row.get("source_type") == "customer_message":
                row["text"] = "[customer message retained in internal archive]"
                row.pop("archive", None)
                row.pop("sender", None)
            rows.append(row)
        return rows


class CompanyIdentityResolver:
    """Score one company candidate using corroborating public evidence."""

    @staticmethod
    def pending(seed):
        identity_id = "company_" + digest([
            seed.get("legal_name", ""), seed.get("official_domain", ""), seed.get("country", "")
        ])[:24]
        return {**seed, "company_identity_id": identity_id, "confidence": 0,
                "status": "pending", "matches": {}, "gaps": []}

    @staticmethod
    def resolve(seed, evidence):
        # Fixture evidence is useful to demonstrate the flow, but can never
        # establish a real-world company identity or support a real claim.
        has_demo_evidence = any(row.get("demo_data") for row in evidence)
        official = [row for row in evidence if row.get("source_type") == "official_website"
                    and row.get("quality") in {"verified_page", "provider_extracted"}]
        maps = [row for row in evidence if row.get("source_type") == "map_lookup"
                and row.get("quality") in {"trusted_api", "directory_api"}]
        has_directory_map = any(row.get("quality") == "directory_api" for row in maps)
        official_domains = [_host(row.get("url")) for row in official]
        map_domains = [_host(row.get("websiteUri") or row.get("website")) for row in maps]
        domains = [value for value in official_domains + map_domains if value]
        expected_domain = seed.get("official_domain", "")
        candidate_domain = next((value for value in domains if value == expected_domain), "")
        if not candidate_domain and domains:
            candidate_domain = max(set(domains), key=domains.count)

        official_text = " ".join(_text(row.get("text") or row.get("title"), 20000) for row in official)
        map_text = " ".join(_text(" ".join(str(value) for value in row.values()), 5000) for row in maps)
        all_text = _normalized(official_text + " " + map_text)
        matches = {}

        name = _normalized(seed.get("legal_name"))
        brand = re.sub(r"(coinc|company|corporation|inc|limited|ltd|llc|有限公司|股份有限公司)$", "", name)
        official_name_match = bool(len(brand) >= 4 and any(
            brand in _normalized((row.get("title") or "") + " " + (row.get("text") or ""))
            for row in official))

        if expected_domain:
            matches["domain"] = bool(candidate_domain and (
                candidate_domain == expected_domain or candidate_domain.endswith("." + expected_domain)))
        else:
            matches["domain"] = bool(set(official_domains).intersection(map_domains) or official_name_match)
        matches["legal_name"] = bool(seed.get("legal_name") and (name in all_text or official_name_match))
        matches["country"] = bool(seed.get("country") and _normalized(seed["country"]) in all_text)

        map_addresses = [_text(row.get("formattedAddress")) for row in maps if row.get("formattedAddress")]
        expected_address = seed.get("address", "")
        matches["address"] = bool(expected_address and _normalized(expected_address) in all_text)
        if not matches["address"] and official_text:
            matches["address"] = any(len(_normalized(value)) >= 12 and _normalized(value) in _normalized(official_text)
                                     for value in map_addresses)

        map_phones = [_digits(row.get("nationalPhoneNumber") or row.get("phone")) for row in maps]
        expected_phone = _digits(seed.get("phone"))
        phones = [value for value in [expected_phone, *map_phones] if len(value) >= 7]
        official_digits = _digits(official_text)
        matches["phone"] = any(value[-7:] in official_digits for value in phones)

        registration = _normalized(seed.get("registration_number"))
        matches["registration_number"] = bool(registration and registration in all_text)
        matches["independent_source"] = bool(maps)
        # An authentic company page can support claims about what that page
        # says even when a map entry or registration record is unavailable.
        # This does not authenticate the person who sent the inquiry.
        matches["public_profile"] = official_name_match and bool(official)
        weights = {"domain": 30, "legal_name": 25, "country": 10,
                   "address": 15, "phone": 10, "registration_number": 10}
        confidence = sum(weight for key, weight in weights.items() if matches.get(key))
        # Missing or weak public evidence calls for human review; it does not
        # prove that the customer-provided company identity is false.
        status = ("verified" if confidence >= 80 and matches["independent_source"]
                  else "needs_review")
        if has_demo_evidence:
            status = "needs_review"
        if has_directory_map and status == "verified":
            status = "needs_review"
        gaps = [key for key, matched in matches.items() if not matched]
        identity_id = "company_" + digest([
            seed.get("legal_name", ""), candidate_domain, seed.get("country", "")
        ])[:24]
        return {**seed, "official_domain": candidate_domain or expected_domain,
                "company_identity_id": identity_id, "confidence": confidence,
                "status": status, "public_profile_verified": bool(matches["public_profile"] and not has_demo_evidence),
                "matches": matches, "gaps": gaps}


class EvidencePolicy:
    SUPPORTED_QUALITIES = {"verified_page", "provider_extracted", "trusted_api"}

    @staticmethod
    def quality(tool, row, result):
        if row.get("demo_data") or result.get("demo_data"):
            return "demo_fixture"
        if tool == "official_website":
            if row.get("extraction_provider") == "tavily_extract" and row.get("sha256"):
                return "provider_extracted"
            return "verified_page" if row.get("status") == "ok" and row.get("sha256") else "failed"
        if tool == "map_lookup":
            if row.get("provider") == "openstreetmap_nominatim":
                return "directory_api" if row.get("id") else "unverified_api_result"
            return "trusted_api" if row.get("id") else "unverified_api_result"
        if tool == "customs_lookup":
            return "trusted_api" if result.get("data_level") == "company_shipment" else "aggregate_data"
        if tool == "news_search":
            if row.get("evidence_quality") == "provider_extracted":
                return "provider_extracted"
            return "verified_page" if row.get("evidence_quality") == "page_fetched" else "lead_only"
        return "unverified"

    @classmethod
    def supports(cls, row, identity):
        brand = re.sub(r"(coinc|company|corporation|inc|limited|ltd|llc|有限公司|股份有限公司)$",
                       "", _normalized((identity or {}).get("legal_name")))
        title_matches = bool(len(brand) >= 4 and brand in _normalized(row.get("title")))
        public_page = bool(identity and identity.get("public_profile_verified") and title_matches
                           and row.get("source_type") in {"official_website", "news_search"})
        return bool(identity and (identity.get("status") == "verified" or public_page)
                    and row.get("company_identity_id") == identity.get("company_identity_id")
                    and row.get("quality") in cls.SUPPORTED_QUALITIES
                    and row.get("source_type") not in {"customer_message", "business_history"})

    @classmethod
    def supports_claim(cls, field, rows, identity):
        eligible = [row for row in rows if cls.supports(row, identity)]
        name = _text(field).casefold()
        if any(token in name for token in ("customs", "shipment", "import", "export", "海关", "提单", "进出口")):
            return any(row.get("source_type") == "customs_lookup"
                       and row.get("data_level") == "company_shipment" for row in eligible)
        if any(token in name for token in ("news", "risk", "lawsuit", "sanction", "新闻", "风险", "诉讼", "制裁")):
            return any(row.get("source_type") == "news_search" for row in eligible)
        if any(token in name for token in ("website", "email", "phone", "address", "contact",
                                            "官网", "邮箱", "电话", "地址", "联系")):
            return any(row.get("source_type") in {"official_website", "map_lookup"} for row in eligible)
        return bool(eligible)
