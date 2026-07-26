from sqlalchemy.orm import Session


class SqlAlchemyUnitOfWork:
    """Thin transaction owner; repositories and domain rules never commit."""

    def __init__(self, session: Session):
        self.session = session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        if exc_type is not None:
            self.rollback()
        return False
