"""Authenticated/audited incident API installed on the existing FastAPI application."""
from fastapi import HTTPException


def install_incident_routes(app, hub):
    @app.get('/control/incidents')
    def incidents():
        return {'items': hub.incidents.public()}

    @app.post('/control/incidents/{incident_id}/ack')
    def acknowledge(incident_id: str):
        if hub.policy.operating_state == 'shutting_down':
            raise HTTPException(409, 'Приложение завершается')
        try:
            return hub.incidents.acknowledge(incident_id)
        except KeyError:
            raise HTTPException(404, 'Обращение не найдено')
