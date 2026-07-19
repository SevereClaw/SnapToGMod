from config import AppConfig, Server
from servers import add_server, clear_non_favorite_servers, record_recent_server, search_servers
from updates import _parse_version


def test_release_version_ordering():
    assert _parse_version("v3.0.0") == (3, 0, 0)
    assert _parse_version("v3.1.0") > _parse_version("3.0.9")


def test_server_management():
    cfg = AppConfig(servers=[Server("Main", "127.0.0.1", "27015")], active_server_name="Main")
    add_server(cfg, "Private", "10.0.0.5", "27016")
    assert cfg.active_server().name == "Private"
    assert search_servers(cfg, "10.0.0") == [cfg.active_server()]


def test_recent_servers_are_unique_and_bounded():
    cfg = AppConfig()
    for index in range(40):
        record_recent_server(cfg, f"Server {index}")
    record_recent_server(cfg, "Server 39")
    assert len(cfg.recent_servers) == 30
    assert cfg.recent_servers[-1] == "Server 39"
    assert len(cfg.recent_servers) == len(set(cfg.recent_servers))


def test_clear_keeps_active_and_favorites():
    cfg = AppConfig(
        servers=[
            Server("Active", "1.1.1.1", "27015"),
            Server("Favorite", "2.2.2.2", "27015", favorite=True),
            Server("Other", "3.3.3.3", "27015"),
        ],
        active_server_name="Active",
    )
    removed = clear_non_favorite_servers(cfg)
    assert removed == 1
    assert {server.name for server in cfg.servers} == {"Active", "Favorite"}
