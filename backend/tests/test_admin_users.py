import uuid

import app.main as main
from test_access import login, server  # noqa: F401  (fixture)


def add_accounts(count: int) -> None:
    """Many accounts without paying for a password hash each."""
    for index in range(count):
        main.database.create_user(
            {
                "id": uuid.uuid4().hex,
                "username": f"user{index:03d}",
                "display_name": f"Person {index:03d}",
                "password_hash": "!test",
                "role": "viewer" if index % 3 else "member",
                "created_at": f"2026-01-01T00:00:{index % 60:02d}+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "email": f"user{index:03d}@example.internal" if index % 2 else None,
            }
        )


def page(client, **params) -> dict:
    response = client.get("/api/admin/users", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_user_list_is_paged(server):  # noqa: F811
    add_accounts(57)  # plus admin, member and viewer from the fixture: 60 accounts
    admin, _ = login("admin")

    first = page(admin)
    assert (first["total"], first["page"], first["per_page"], first["pages"]) == (60, 1, 25, 3)
    assert len(first["items"]) == 25
    assert first["items"][0]["username"] == "admin"  # administrators first by default

    seen = []
    for number in (1, 2, 3, 4, 5, 6):
        seen += [row["username"] for row in page(admin, per_page=10, page=number)["items"]]
    assert len(seen) == len(set(seen)) == 60

    # A page past the end answers with the last page instead of an empty list.
    beyond = page(admin, per_page=25, page=9)
    assert beyond["page"] == 3 and len(beyond["items"]) == 10

    for bad in ({"per_page": 0}, {"per_page": 101}, {"page": 0}, {"role": "owner"}, {"sort": "password_hash"}):
        assert admin.get("/api/admin/users", params=bad).status_code == 422, bad


def test_admin_user_list_filters_and_counts(server):  # noqa: F811
    add_accounts(57)
    admin, _ = login("admin")
    member_id = main.database.get_user_by_username("user001")["id"]
    assert admin.patch(f"/api/admin/users/{member_id}", json={"disabled": True}).status_code == 200

    everyone = page(admin)
    assert everyone["counts"] == {
        "all": 60, "admin": 1, "member": 20, "viewer": 39, "active": 59, "disabled": 1,
    }

    # Search matches username, display name, or email, ignoring case.
    assert [row["username"] for row in page(admin, q="USER01", sort="name")["items"]] == [f"user01{n}" for n in range(10)]
    assert page(admin, q="person 042")["items"][0]["username"] == "user042"
    assert page(admin, q="user043@example")["total"] == 1
    assert page(admin, q="user044@example")["total"] == 0  # even rows have no email
    # LIKE wildcards in the search box are matched literally.
    assert page(admin, q="%")["total"] == 0
    assert page(admin, q="_")["total"] == 0

    searched = page(admin, q="user01")
    assert searched["counts"]["all"] == 10 and searched["total"] == 10

    viewers = page(admin, role="viewer", per_page=100)
    assert viewers["total"] == 39 and {row["role"] for row in viewers["items"]} == {"viewer"}
    disabled = page(admin, status="disabled")
    assert [row["username"] for row in disabled["items"]] == ["user001"]
    assert page(admin, role="member", status="active")["total"] == 20

    by_name = [row["username"] for row in page(admin, sort="name", per_page=5)["items"]]
    assert by_name == ["admin", "member", "user000", "user001", "user002"]
    member, _ = login("member")
    recent = page(admin, sort="last_login", per_page=3)["items"]
    assert [row["username"] for row in recent[:2]] == ["member", "admin"]

    # Activity counts are still reported for the accounts on the page.
    row = next(row for row in page(admin, q="member", role="member")["items"] if row["username"] == "member")
    assert row["repositories"] == 1 and row["sessions"] == 1
