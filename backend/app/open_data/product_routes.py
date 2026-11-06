"""Fixed internal reads for persisted, independently collected fund products."""
from __future__ import annotations

import json

from flask import jsonify, request

from .product_contracts import contracts
from .product_store import query_product


def register_product_routes(bp):
    """Register onto the existing JWT-authenticated, read-only namespace."""
    @bp.get("/fund-products/<int:product>")
    def read_fund_product(product):  # pylint: disable=too-many-return-statements
        if product not in contracts():
            return jsonify({"error": "unknown_product"}), 404
        if set(request.args) != {"query_json"} or len(request.args.getlist("query_json")) != 1:
            return jsonify({"error": "invalid_request"}), 400
        text = request.args["query_json"]
        if len(text.encode()) > 16384:
            return jsonify({"error": "invalid_request"}), 400
        try:
            query = json.loads(text)
            if not isinstance(query, dict):
                raise ValueError("Expected object")
            if product in {142, 143}:
                from .product_etf_generation import query as etf_query  # pylint: disable=import-outside-toplevel
                return jsonify(etf_query(product, query))
            if product == 252:
                from .product_manager_history import query as history_query  # pylint: disable=import-outside-toplevel
                return jsonify(history_query(query))
            if product in {263, 267}:
                from .product_concepts import query as concept_query  # pylint: disable=import-outside-toplevel
                return jsonify(concept_query(product, query))
            return jsonify(query_product(product, query))
        except (ValueError, KeyError, TypeError):
            return jsonify({"error": "invalid_request"}), 400
        except LookupError:
            return jsonify({"error": "product_window_incomplete", "reason": "source_window_not_verified"}), 503
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if "no such table" in str(exc).lower() or (exc.args and exc.args[0] == 1146):
                return jsonify({"error": "product_window_incomplete", "reason": "explicit_migration_required"}), 503
            return jsonify({"error": "data_source_unavailable"}), 503
