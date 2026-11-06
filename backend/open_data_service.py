"""Dedicated production read service; no startup migration or personal blueprints."""
from __future__ import annotations

import datetime
import os

from flask import Flask, jsonify
from flask_jwt_extended import JWTManager


def create_app():
    """Only existing authentication and bounded source APIs are registered."""
    secret = os.getenv('SECRET_KEY', '')
    if len(secret) < 32 or secret in {'dev-secret', 'change-me'}:
        raise RuntimeError('A strong server-side SECRET_KEY of at least 32 characters is mandatory')
    app = Flask(__name__)
    app.config.update(JWT_SECRET_KEY=secret, JWT_TOKEN_LOCATION=['headers'],
                      JWT_ACCESS_TOKEN_EXPIRES=datetime.timedelta(hours=1))
    JWTManager(app)
    from app.routers.auth import exchange_token  # pylint: disable=import-outside-toplevel
    from app.open_data.router import bp as source  # pylint: disable=import-outside-toplevel
    app.add_url_rule('/api/auth/token/exchange', view_func=exchange_token, methods=['POST'])
    app.register_blueprint(source)

    @app.get('/api/health')
    def health():
        return jsonify({'service': 'ifund-open-data', 'status': 'alive'})
    return app


if __name__ == '__main__':
    from waitress import serve
    serve(create_app(), host='0.0.0.0', port=int(os.getenv('PORT', '8000')), threads=4,
          channel_timeout=30, max_request_body_size=1048576)
