"""문서 권한 추가를 위해 `connection.scopes` 갱신 권한을 연다 (ADR-0020 §1)

Revision ID: 0009
Revises: 0008

- 0006은 `scopes`를 바꿀 수 없게 했다(바꾸려면 새 연결). 문서(Drive) 읽기 권한을 추가할 때는 같은
  행의 `scopes`와 토큰 암호문을 바꿔야 새 연결이 생기지 않고 메일 색인(ADR-0019)이 유지된다.
- 토큰 열(`token_ciphertext`·`token_nonce`·`wrapped_dek`·`kek_id`)과 `updated_at`의 UPDATE 권한은
  0006에 이미 있다. 여기서는 `scopes` 한 열만 더 연다. `account_*`는 계속 바꿀 수 없다.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT UPDATE (scopes) ON connection TO orbit_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (scopes) ON connection FROM orbit_app")
