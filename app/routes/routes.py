import json
import os
import re
from flask import Flask, Response, jsonify, render_template, request, redirect, url_for, session, flash, Blueprint, g
from app import app, db, functions, sp, jellyfin, celery, jellyfin_admin_token, jellyfin_admin_id,device_id,  cache, read_dev_build_file, tasks
from app.classes import AudioProfile, CombinedPlaylistData
from app.models import JellyfinUser,Playlist,Track
from celery.result import AsyncResult
from typing import List

from app.providers import base
from app.providers.base import MusicProviderClient
from app.providers.spotify import SpotifyClient
from app.registry.music_provider_registry import MusicProviderRegistry
from ..version import __version__
from spotipy.exceptions import SpotifyException
from collections import defaultdict
from app.routes import pl_bp


@app.context_processor
def add_context():
    unlinked_track_count = len(Track.query.filter_by(downloaded=True,jellyfin_id=None).all())
    version = f"v{__version__}{read_dev_build_file()}"
    return dict(unlinked_track_count = unlinked_track_count, version = version, config = app.config , registered_providers = MusicProviderRegistry.list_providers())


# this feels wrong 
skip_endpoints = ['task_status']
@app.after_request
def render_messages(response: Response) -> Response:
    if request.headers.get("HX-Request"):
        if request.endpoint not in skip_endpoints:
            messages = render_template("partials/alerts.jinja2")
            response.headers['HX-Trigger'] = 'showToastMessages'
            response.data = response.data + messages.encode("utf-8")
    return response



@app.route('/admin/tasks')
@functions.jellyfin_admin_required
def task_manager():
    statuses = {}
    for task_name, task_id in functions.TASK_STATUS.items():
        if task_id:
            result = AsyncResult(task_id)
            statuses[task_name] = {'state': result.state, 'info': result.info if result.info else {}}
        else:
            statuses[task_name] = {'state': 'NOT STARTED', 'info': {}}
    
    return render_template('admin/tasks.html', tasks=statuses,lock_keys  = functions.LOCK_KEYS)

@app.route('/admin')
@app.route('/admin/link_issues')
@functions.jellyfin_admin_required
def link_issues():
    # add the ability to pass a query parameter to dislplay even undownloaded tracks
    list_undownloaded = request.args.get('list_undownloaded')
    if list_undownloaded:
        unlinked_tracks = Track.query.filter_by(jellyfin_id=None).all()
    else:
        unlinked_tracks = Track.query.filter_by(downloaded=True,jellyfin_id=None).all()
    tracks = []
    for ult in unlinked_tracks: 
        provider_track = functions.get_cached_provider_track(ult.provider_track_id, ult.provider_id)
        duration_ms = provider_track.duration_ms
        minutes = duration_ms // 60000
        seconds = (duration_ms % 60000) // 1000
        tracks.append({
                'title': provider_track.name,
                'artist': ', '.join([artist.name for artist in provider_track.artists]),
                'url': provider_track.external_urls,
                'duration': f'{minutes}:{seconds:02d}', 
                'preview_url': '',
                'downloaded': ult.downloaded,  
                'filesystem_path': ult.filesystem_path,  
                'jellyfin_id': ult.jellyfin_id,
                'provider_track_id': provider_track.id,
                'duration_ms': duration_ms,
                'download_status'  : ult.download_status,
                'provider_id' : ult.provider_id
            })

    return render_template('admin/link_issues.html' , tracks = tracks )



@app.route('/run_task/<task_name>', methods=['POST'])
@functions.jellyfin_admin_required
def run_task(task_name):
    status, info = functions.manage_task(task_name)
    
    # Rendere nur die aktualisierte Zeile der Task
    task_info = {task_name: {'state': status, 'info': info}}
    return render_template('partials/_task_status.html', tasks=task_info)


@app.route('/task_status')
@functions.jellyfin_admin_required
def task_status():
    statuses = {}
    for task_name, task_id in functions.TASK_STATUS.items():
        if task_id:
            result = AsyncResult(task_id)
            statuses[task_name] = {'state': result.state, 'info': result.info if result.info else {}}
        else:
            statuses[task_name] = {'state': 'NOT STARTED', 'info': {}}

    # Render the HTML partial template instead of returning JSON
    return render_template('partials/_task_status.html', tasks=statuses)



@app.route('/')
@functions.jellyfin_login_required 
def index():
    users = JellyfinUser.query.all()
    return render_template('index.html', user=session['jellyfin_user_name'], users=users)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        try:
            jellylogin = jellyfin.login_with_password(username=username, password=password)
            if jellylogin:
                session['jellyfin_access_token'], session['jellyfin_user_id'], session['jellyfin_user_name'],session['is_admin'] = jellylogin
                session['debug'] = app.debug
                # Check if the user already exists
                user = JellyfinUser.query.filter_by(jellyfin_user_id=session['jellyfin_user_id']).first()
                if not user:
                    # Add the user to the database if they don't exist
                    new_user = JellyfinUser(name=session['jellyfin_user_name'], jellyfin_user_id=session['jellyfin_user_id'], is_admin = session['is_admin'])
                    db.session.add(new_user)
                    db.session.commit()

                return redirect('/')
        except:
            flash('Login failed. Please check your Jellyfin credentials and try again.', 'error')
            return redirect(url_for('login'))

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('jellyfin_user_name', None)
    session.pop('jellyfin_access_token', None)
    return redirect(url_for('login'))

@app.route('/playlist/open',methods=['GET'])
@functions.jellyfin_login_required
def openPlaylist():
    playlist = request.args.get('playlist')
    error = None
    errdata= None
    if playlist:
        for provider_id in MusicProviderRegistry.list_providers():
            try:
                provider_client = MusicProviderRegistry.get_provider(provider_id)
                extracted_playlist_id = provider_client.extract_playlist_id(playlist)
                provider_playlist = provider_client.get_playlist(extracted_playlist_id)
                
                combined_data = functions.prepPlaylistData(provider_playlist)
                if combined_data:
                    # If the playlist is found, redirect to the playlist view, but also include the provider ID in the URL
                    return redirect(url_for('playlist.get_playlist_tracks', playlist_id=extracted_playlist_id, provider=provider_id))
            except Exception as e:
                error = f"Error fetching playlist from {provider_id}: {str(e)}"
                errdata = e
            
    return render_template('index.html',error_message = error, error_data = errdata)

@pl_bp.route('/browse')
@functions.jellyfin_login_required
def browse():
    provider: MusicProviderClient = g.music_provider  

    browse_data = provider.browse()
    return render_template('browse.html', browse_data=browse_data,provider_id=provider._identifier)

@pl_bp.route('/browse/page/<page_id>')
@functions.jellyfin_login_required
def browse_page(page_id):
    provider: MusicProviderClient = g.music_provider  
    combined_playlist_data : List[CombinedPlaylistData] = []
    
    data = provider.browse_page(page_id)
    for item in data:
        cpd = functions.prepPlaylistData(item)
        if cpd:
            combined_playlist_data.append(cpd)
    return render_template('browse_page.html', data=combined_playlist_data,provider_id=provider._identifier)

@pl_bp.route('/playlists/monitored')
@functions.jellyfin_login_required
def monitored_playlists():

    # 1. Get all Playlists from the Database.
    all_playlists = Playlist.query.all()

    # 2. Group them by provider
    playlists_by_provider = defaultdict(list)
    for playlist in all_playlists:
        playlists_by_provider[playlist.provider_id].append(playlist)

    provider_playlists_data = {}
    # 3. Fetch all Data from the provider using the get_playlist() method 
    for provider_id, playlists in playlists_by_provider.items():
        try:
            provider_client = MusicProviderRegistry.get_provider(provider_id)
        except ValueError:
            flash(f"Provider {provider_id} not found.", "error")
            continue

        combined_playlists = []
        for pl in playlists:
            provider_playlist = provider_client.get_playlist(pl.provider_playlist_id)
            # 4. Convert the playlists to CombinedPlaylistData
            combined_data = functions.prepPlaylistData(provider_playlist)
            if combined_data:
                combined_playlists.append(combined_data)

        provider_playlists_data[provider_id] = combined_playlists

    # 5. Display the resulting Groups in a template called 'monitored_playlists.html', one Heading per Provider
    return render_template('monitored_playlists.html', provider_playlists_data=provider_playlists_data, title="Monitored Playlists", subtitle="Playlists which are already monitored by Jellyplist and are available immediately")

@app.route('/search')
@functions.jellyfin_login_required
def searchResults():
    query = request.args.get('query')
    context = {}
    if query:
        #iterate through every registered music provider and perform the search with it.
        # Group the results by provider and display them using monitorerd_playlists.html
        search_results = defaultdict(list)
        for provider_id in MusicProviderRegistry.list_providers():
            try:
                provider_client = MusicProviderRegistry.get_provider(provider_id)
                results = provider_client.search_playlist(query)
                for result in results:
                    search_results[provider_id].append(result)
            except Exception as e:
                flash(f"Error fetching search results from {provider_id}: {str(e)}", "error")
        # the grouped search results, must be prepared using the prepPlaylistData function
        for provider_id, playlists in search_results.items():
            combined_playlists = []
            for pl in playlists:
                combined_data = functions.prepPlaylistData(pl)
                if combined_data:
                    combined_playlists.append(combined_data)
            search_results[provider_id] = combined_playlists
            
        context['provider_playlists_data'] = search_results
        context['title'] = 'Search Results'
        context['subtitle'] = 'Search results from all providers'
    return render_template('monitored_playlists.html', **context)

@pl_bp.route('/track_details/<track_id>')
@functions.jellyfin_login_required
def track_details(track_id):
    provider_id = request.args.get('provider')
    if not provider_id:
        return jsonify({'error': 'Provider not specified'}), 400

    track = Track.query.filter_by(provider_track_id=track_id, provider_id=provider_id).first()
    if not track:
        return jsonify({'error': 'Track not found'}), 404

    provider_track = functions.get_cached_provider_track(track.provider_track_id, track.provider_id)
    # query also this track using the jellyfin id directly from jellyfin 
    if track.jellyfin_id:
        jellyfin_track = jellyfin.get_item(session_token=functions._get_api_token(), item_id=track.jellyfin_id)
        if jellyfin_track:
            jellyfin_filesystem_path = jellyfin_track['Path']
    duration_ms = provider_track.duration_ms
    minutes = duration_ms // 60000
    seconds = (duration_ms % 60000) // 1000

    track_details = {
        'title': provider_track.name,
        'artist': ', '.join([artist.name for artist in provider_track.artists]),
        'url': provider_track.external_urls,
        'duration': f'{minutes}:{seconds:02d}', 
        'downloaded': track.downloaded,  
        'filesystem_path': track.filesystem_path,  
        'jellyfin_id': track.jellyfin_id,
        'provider_track_id': provider_track.id,
        'provider_track_url': provider_track.external_urls[0].url if provider_track.external_urls else None,
        'duration_ms': duration_ms,
        'download_status': track.download_status,
        'provider_id': track.provider_id,
        'jellyfin_filesystem_path': jellyfin_filesystem_path if track.jellyfin_id else None,
    }

    return render_template('partials/track_details.html', track=track_details)

@pl_bp.route('/playlist/view/<playlist_id>')
@functions.jellyfin_login_required
def get_playlist_tracks(playlist_id):
    provider: MusicProviderClient = g.music_provider  
    playlist: base.Playlist = provider.get_playlist(playlist_id)
    tracks = functions.get_tracks_for_playlist(playlist.tracks, provider_id=provider._identifier)  
    total_duration_ms = sum([track.duration_ms for track in tracks])

    # Convert the total duration to a readable format
    hours, remainder = divmod(total_duration_ms // 1000, 3600)
    minutes, seconds = divmod(remainder, 60)

    # Format the duration
    if hours > 0:
        total_duration = f"{hours}h {minutes}min"
    else:
        total_duration = f"{minutes}min"
    
    return render_template(
        'tracks_table.html',
        tracks=tracks,
        total_duration=total_duration,
        track_count=len(tracks),
        provider_id = provider._identifier,
        item=functions.prepPlaylistData(playlist),
        
    )

@app.route('/associate_track', methods=['POST'])
@functions.jellyfin_login_required
def associate_track():
    jellyfin_id = request.form.get('jellyfin_id')
    provider_track_id = request.form.get('provider_track_id')

    if not jellyfin_id or not provider_track_id:
        flash('Missing Jellyfin or Spotify ID')

    # Retrieve the track by Spotify ID
    track = Track.query.filter_by(provider_track_id=provider_track_id).first()

    if not track:
        flash('Track not found')
        return ''

    # Associate the Jellyfin ID with the track
    track.jellyfin_id = jellyfin_id
    track.downloaded = True
    

    try:
        # Commit the changes to the database
        db.session.commit()
        flash("Track associated","success")
        return ''
    except Exception as e:
        db.session.rollback()  # Roll back the session in case of an error
        flash(str(e))
        return ''
        

@app.route("/unlock_key",methods = ['POST'])
@functions.jellyfin_admin_required
def unlock_key():
     
    key_name = request.form.get('inputLockKey')
    if key_name:
        tasks.release_lock(key_name)
        flash(f'Lock {key_name} released', category='success')
    return ''


@pl_bp.route('/test')
def test():
    #return '' 
    app.logger.info(f"performing full update on jellyfin track ids. (Update tracks and playlists if better quality will be found)")
    downloaded_tracks : List[Track] = Track.query.all()
    total_tracks = len(downloaded_tracks)
    if not downloaded_tracks:
        app.logger.info("No downloaded tracks without Jellyfin ID found.")
        return {'status': 'No tracks to update'}

    app.logger.info(f"Found {total_tracks} tracks to update ")
    processed_tracks = 0

    for track in downloaded_tracks:
        try:
            best_match = tasks.find_best_match_from_jellyfin(track)
            if best_match:
                track.downloaded = True
                if track.jellyfin_id != best_match['Id']:
                    track.jellyfin_id = best_match['Id']
                    app.logger.info(f"Updated Jellyfin ID for track: {track.name} ({track.provider_track_id})")
                if track.filesystem_path != best_match['Path']:
                    track.filesystem_path = best_match['Path']
                    app.logger.info(f"Updated filesystem_path for track: {track.name} ({track.provider_track_id})")
                    
                    
                
                db.session.commit()
            else:
                app.logger.warning(f"No matching track found in Jellyfin for {track.name}.")
            
            spotify_track = None
            
        except Exception as e:
            app.logger.error(f"Error searching Jellyfin for track {track.name}: {str(e)}")

        processed_tracks += 1
        progress = (processed_tracks / total_tracks) * 100
        #self.update_state(state=f'{processed_tracks}/{total_tracks}: {track.name}', meta={'current': processed_tracks, 'total': total_tracks, 'percent': progress})

    app.logger.info("Finished updating Jellyfin IDs for all tracks.")
    return {'status': 'All tracks updated', 'total': total_tracks, 'processed': processed_tracks}