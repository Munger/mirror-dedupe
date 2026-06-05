## @file html_helpers.py
##
## @brief Generic HTML/Markdown helper functions for mirror-dedupe.
##
## This module provides small, reusable helpers for extracting links and
## path segments from mixed HTML / Markdown directory listings such as
## APT ``/dists/`` indexes.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse, urlunparse


def url_protocol(url: str) -> str:
    ## @brief Return the URL scheme (protocol), e.g. "http" or "https".
    ## @param url  URL to extract the protocol from.
    ## @return The URL scheme as a string.

    return urlparse(url).scheme


def url_host(url: str) -> str:
    ## @brief Return the hostname (and optional port) component of *url*.
    ## @param url  URL to extract the host from.
    ## @return The netloc (host:port) component.

    parsed = urlparse(url)
    return parsed.netloc


def url_hostname(url: str) -> str:
    ## @brief Return only the hostname portion of *url* (without port).
    ## @param url  URL to extract the hostname from.
    ## @return The hostname string, or empty string if not found.

    return urlparse(url).hostname or ""


def url_path(url: str) -> str:
    ## @brief Return the path component of *url*, without query or fragment.
    ## @param url  URL to extract the path from.
    ## @return The path component as a string.

    return urlparse(url).path


def build_url(base: str, *parts: str) -> str:
    ## @brief Build a URL from *base* and additional path segments.
    ##
    ## All segments are joined with ``/`` with any leading/trailing slashes
    ## stripped, preserving the original scheme/host/query/fragment of
    ## *base* while replacing only the path component.
    ##
    ## @param base   Base URL (scheme, host, optional path).
    ## @param parts  Additional path segments to append.
    ## @return The constructed URL.

    parsed = urlparse(base)
    base_path = parsed.path.rstrip("/")
    subpath = "/".join(p.strip("/") for p in parts if p)
    new_path = f"{base_path}/{subpath}" if subpath else base_path
    return urlunparse(parsed._replace(path=new_path))


def extract_href(line: str) -> Optional[str]:
    ## @brief Return the first link URL in *line*.
    ##
    ## This understands both classic HTML ``href="..."`` attributes and
    ## Markdown-style ``[text](url)`` links. Callers are responsible for
    ## applying any policy-specific filtering to the returned URL.
    ##
    ## @param line  A line of HTML or Markdown text.
    ## @return The extracted URL, or None if no link was found.

    href: Optional[str] = None

    # Prefer HTML: <a href="...">.
    if 'href="' in line:
        start = line.find('href="') + 6
        end = line.find('"', start)
        if start > 5 and end > start:
            href = line[start:end]

    # Fallback: Markdown-style [...](URL).
    elif "](" in line and ")" in line:
        open_paren = line.find("](")
        close_paren = line.find(")", open_paren + 2)
        if open_paren >= 0 and close_paren > open_paren + 2:
            href = line[open_paren + 2 : close_paren]

    return href


def extract_last_path_segment(line: str) -> Optional[str]:
    ## @brief Return the last non-empty path segment from the first link in *line*.
    ##
    ## This is a thin wrapper around :func:`extract_href`. It splits the
    ## extracted URL on ``/`` and returns the final non-empty component, or
    ## ``None`` if no such component exists.
    ##
    ## @param line  A line of HTML or Markdown text.
    ## @return The last path segment, or None.

    href = extract_href(line)
    if not href:
        return None

    parts = [p for p in href.split("/") if p]
    if not parts:
        return None

    return parts[-1]
