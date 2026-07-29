"""Backward-compatible entry point for deployments that used import_faces.py.

Employee reference images are no longer downloaded to attendance servers. This
command now synchronizes the validated embedding gallery instead.
"""

from sync_embeddings import main


if __name__ == "__main__":
    main()
