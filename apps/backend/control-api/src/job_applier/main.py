"""Application entrypoints for the bootstrap project."""

from job_applier.interface.http.app import create_app

app = create_app()
