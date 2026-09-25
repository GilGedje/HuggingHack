import app.main as main
from test_access import server  # noqa: F401  (fixture)
from test_organizations import add_user, login, org, upload  # noqa: F401  (fixtures)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x40\x00\x00\x00WEBPVP8 " + b"\x00" * 64


def put(client, path: str, body: bytes, content_type: str = "image/png"):
    return client.put(path, content=body, headers={"Content-Type": content_type})


def test_a_picture_is_what_its_bytes_say_and_nothing_else(org):  # noqa: F811
    writer, _ = login("writer")
    for body, media_type in ((PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")):
        saved = put(writer, "/api/account/avatar", body, "image/png")
        assert saved.status_code == 200, saved.text
        # Any spelling of the name finds it; the stored type comes from the bytes.
        picture = writer.get(saved.json()["avatar"].replace("/writer?", "/WRITER?"))
        assert picture.status_code == 200 and picture.content == body
        assert picture.headers["content-type"] == media_type
        assert picture.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in picture.headers["content-security-policy"]
        assert "immutable" in picture.headers["cache-control"]
    assert writer.get("/api/avatars/writer").headers["cache-control"] == "private, no-cache"
    assert writer.get("/api/auth/status").json()["user"]["avatar_updated_at"]

    for body in (b"<html><script>alert(1)</script></html>", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b""):
        assert put(writer, "/api/account/avatar", body).status_code == 400
    assert put(writer, "/api/account/avatar", PNG + b"\x00" * (512 * 1024)).status_code == 413

    assert writer.delete("/api/account/avatar").status_code == 200
    assert writer.get("/api/avatars/writer").status_code == 404
    assert not list((main.settings.data_dir / "system" / "avatars" / "users").iterdir())


def test_only_organization_admins_change_its_picture(org):  # noqa: F811
    for username in ("writer", "reader", "outsider"):
        client, _ = login(username)
        assert put(client, "/api/organizations/nvidia/avatar", PNG).status_code == 403, username
    admin, _ = login("admin")  # created the organization, so it is its admin
    saved = put(admin, "/api/organizations/Nvidia/avatar", PNG)
    assert saved.status_code == 200, saved.text
    assert saved.json()["avatar"].startswith("/api/avatars/Nvidia?v=")

    # Model cards and pages carry the owner's picture, from one lookup per page.
    writer, _ = login("writer")
    assert writer.post("/api/uploads/repositories", json={"slug": "pic", "namespace": "Nvidia", "visibility": "public"}).status_code == 201
    upload(writer, "Nvidia/pic", {"config.json": b"{}"})
    item = next(item for item in writer.get("/api/library/models").json()["items"] if item["id"] == "Nvidia/pic")
    assert item["author_avatar"] == saved.json()["avatar"]
    assert writer.get("/api/library/models/Nvidia/pic").json()["author_avatar"] == saved.json()["avatar"]
    assert writer.get(item["author_avatar"]).content == PNG
    assert writer.get("/api/organizations/nvidia").json()["avatar_updated_at"]

    # Deleting the organization deletes its picture.
    admin.request("DELETE", "/api/repos", params={"repo_id": "Nvidia/pic"}, json={"confirmation": "Nvidia/pic"})
    organization = main.database.get_organization("nvidia")
    assert admin.delete("/api/organizations/nvidia").status_code == 200
    assert not (main.settings.data_dir / "system" / "avatars" / "organizations" / organization["id"]).exists()


def test_deleting_an_account_deletes_its_picture(org):  # noqa: F811
    add_user(org, "leaving", "member")
    leaving, status = login("leaving")
    assert put(leaving, "/api/account/avatar", PNG).status_code == 200
    path = main.settings.data_dir / "system" / "avatars" / "users" / status["user"]["id"]
    assert path.exists()
    admin, _ = login("admin")
    assert admin.delete(f"/api/admin/users/{status['user']['id']}").status_code == 200
    assert not path.exists()
