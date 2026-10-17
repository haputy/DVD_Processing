import pytest
from unittest.mock import patch, MagicMock
from dvd_processor.tmdb import TmdbClient, TmdbShow, TmdbEpisode


@pytest.fixture
def client():
    return TmdbClient(api_key="test_key")


def fake_search_response():
    return {
        "results": [
            {"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17"},
            {"id": 9999, "name": "Game of Thrones (2020)", "first_air_date": "2020-01-01"},
        ]
    }


def fake_season_response():
    return {
        "episodes": [
            {"episode_number": 1, "name": "Winter Is Coming", "runtime": 62},
            {"episode_number": 2, "name": "The Kingsroad", "runtime": 56},
            {"episode_number": 3, "name": "Lord Snow", "runtime": 58},
        ]
    }


def test_search_returns_show_list(client):
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        mock_get.return_value.json.return_value = fake_search_response()
        mock_get.return_value.raise_for_status = MagicMock()
        results = client.search_show("Game of Thrones")
    assert len(results) == 2
    assert results[0].name == "Game of Thrones"
    assert results[0].tmdb_id == 1399
    assert results[0].year == "2011"


def test_get_episodes_returns_episode_list(client):
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        mock_get.return_value.json.return_value = fake_season_response()
        mock_get.return_value.raise_for_status = MagicMock()
        episodes = client.get_season_episodes(tmdb_id=1399, season=1)
    assert len(episodes) == 3
    assert episodes[0].number == 1
    assert episodes[0].title == "Winter Is Coming"
    assert episodes[0].runtime_secs == 62 * 60


def test_get_episodes_none_runtime_returns_none(client):
    response = {"episodes": [{"episode_number": 1, "name": "Ep 1", "runtime": None}]}
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        mock_get.return_value.json.return_value = response
        mock_get.return_value.raise_for_status = MagicMock()
        episodes = client.get_season_episodes(tmdb_id=1399, season=1)
    assert episodes[0].runtime_secs is None


def _respond(mock_get, payload):
    mock_get.return_value.json.return_value = payload
    mock_get.return_value.raise_for_status = MagicMock()


def test_get_episodes_includes_overview(client):
    response = {"episodes": [{"episode_number": 1, "name": "Pilot", "runtime": 22,
                              "overview": "Dave arrives at WNYX."}]}
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        _respond(mock_get, response)
        episodes = client.get_season_episodes(tmdb_id=1, season=1)
    assert episodes[0].overview == "Dave arrives at WNYX."


def test_get_show_details_skips_specials(client):
    response = {"id": 3, "name": "NewsRadio", "first_air_date": "1995-03-21",
                "number_of_episodes": 97,
                "seasons": [{"season_number": 0}, {"season_number": 2}, {"season_number": 1}]}
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        _respond(mock_get, response)
        show = client.get_show(3)
    assert show.name == "NewsRadio"
    assert show.year == "1995"
    assert show.seasons == [1, 2]
    assert show.number_of_episodes == 97


def test_get_regulars_keeps_main_cast_first_names(client):
    response = {"cast": [
        {"total_episode_count": 97, "roles": [{"character": "Dave Nelson"}]},
        {"total_episode_count": 90, "roles": [{"character": "Bill McNeal / Narrator (voice)"}]},
        {"total_episode_count": 60, "roles": [{"character": "Dr. Lisa Miller"}]},
        {"total_episode_count": 2, "roles": [{"character": "Guest Person"}]},
        {"total_episode_count": 50, "roles": [{"character": "Himself"}]},
    ]}
    with patch("dvd_processor.tmdb.requests.get") as mock_get:
        _respond(mock_get, response)
        regulars = client.get_regulars(3)
    assert regulars == ["Dave", "Bill", "Lisa"]
