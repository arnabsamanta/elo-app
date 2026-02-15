import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Config
app.config['SECRET_KEY'] = 'mysecretkey123'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_db.sqlite')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)

# --- MODELS ---

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)

class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)

class GroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    elo_rating = db.Column(db.Integer, default=1200)

    user = db.relationship('User', backref='memberships')
    group = db.relationship('Group', backref='members')

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    winner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    loser_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    score = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    winner = db.relationship('User', foreign_keys=[winner_id])
    loser = db.relationship('User', foreign_keys=[loser_id])

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- ELO LOGIC ---

def calculate_elo_change(winner_elo, loser_elo):
    K = 32
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))

    new_winner = winner_elo + K * (1 - expected_winner)
    new_loser = loser_elo + K * (0 - expected_loser)

    return round(new_winner), round(new_loser)

def recalculate_group_elos(group_id):
    # 1. Reset all members to 1200
    members = GroupMember.query.filter_by(group_id=group_id).all()
    elo_map = {m.user_id: 1200 for m in members}

    # 2. Get all matches sorted by time
    matches = Match.query.filter_by(group_id=group_id).order_by(Match.timestamp.asc()).all()

    # 3. Replay history
    for m in matches:
        if m.winner_id in elo_map and m.loser_id in elo_map:
            w_elo = elo_map[m.winner_id]
            l_elo = elo_map[m.loser_id]
            nw, nl = calculate_elo_change(w_elo, l_elo)
            elo_map[m.winner_id] = nw
            elo_map[m.loser_id] = nl

    # 4. Save to DB
    for m in members:
        m.elo_rating = elo_map.get(m.user_id, 1200)
    db.session.commit()

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username taken'}), 400
    hashed_pw = generate_password_hash(data['password'], method='pbkdf2:sha256')
    new_user = User(username=data['username'], password=hashed_pw)
    db.session.add(new_user)
    db.session.commit()
    return jsonify({'message': 'Created'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if user and check_password_hash(user.password, data['password']):
        login_user(user)
        return jsonify({'message': 'Logged in', 'username': user.username, 'id': user.id})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout')
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'Logged out'})

@app.route('/api/groups', methods=['GET', 'POST'])
@login_required
def groups():
    if request.method == 'POST':
        data = request.json
        new_group = Group(name=data['name'])
        db.session.add(new_group)
        db.session.commit()
        member = GroupMember(user_id=current_user.id, group_id=new_group.id, is_admin=True)
        db.session.add(member)
        db.session.commit()
        return jsonify({'message': 'Group created'})

    memberships = GroupMember.query.filter_by(user_id=current_user.id).all()
    group_list = [{'id': m.group.id, 'name': m.group.name, 'is_admin': m.is_admin} for m in memberships]
    return jsonify(group_list)

@app.route('/api/groups/<int:group_id>/details')
@login_required
def group_details(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership:
        return jsonify({'error': 'Unauthorized'}), 403

    # 1. Matches List
    matches = Match.query.filter_by(group_id=group_id).order_by(Match.timestamp.desc()).all()
    matches_data = [{
        'id': m.id,
        'winner': m.winner.username,
        'winner_id': m.winner_id,
        'loser': m.loser.username,
        'loser_id': m.loser_id,
        'score': m.score,
        'date': m.timestamp.strftime('%Y-%m-%d %H:%M')
    } for m in matches]

    # 2. Leaderboard
    members = GroupMember.query.filter_by(group_id=group_id).order_by(GroupMember.elo_rating.desc()).all()
    leaderboard_data = [{
        'username': m.user.username, 'elo': m.elo_rating, 'is_admin': m.is_admin, 'id': m.user_id
    } for m in members]

    # 3. Graph History (Closing Elo per Day)
    history_matches = Match.query.filter_by(group_id=group_id).order_by(Match.timestamp.asc()).all()

    elo_map = {m.user_id: 1200 for m in members}
    user_names = {m.user_id: m.user.username for m in members}

    # Dictionary to store closing elo: { user_id: { 'YYYY-MM-DD': elo } }
    daily_closing_elos = {uid: {} for uid in elo_map}

    for m in history_matches:
        if m.winner_id in elo_map and m.loser_id in elo_map:
            w_elo = elo_map[m.winner_id]
            l_elo = elo_map[m.loser_id]
            nw, nl = calculate_elo_change(w_elo, l_elo)

            elo_map[m.winner_id] = nw
            elo_map[m.loser_id] = nl

            # Use date string as key. Overwriting ensures we keep the last (closing) elo of the day.
            day_key = m.timestamp.strftime('%Y-%m-%d')
            daily_closing_elos[m.winner_id][day_key] = nw
            daily_closing_elos[m.loser_id][day_key] = nl

    # Format for Chart.js
    chart_datasets = []
    colors = ['#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF', '#FF9F40']

    for idx, (uid, date_map) in enumerate(daily_closing_elos.items()):
        # Convert dict to list of {x, y} sorted by date
        data_points = []
        sorted_dates = sorted(date_map.keys())

        for d in sorted_dates:
            data_points.append({'x': d, 'y': date_map[d]})

        if data_points: # Only add players who have played
            chart_datasets.append({
                'label': user_names.get(uid, 'Unknown'),
                'data': data_points,
                'borderColor': colors[idx % len(colors)],
                'fill': False,
                'tension': 0.1
            })

    return jsonify({
        'matches': matches_data,
        'leaderboard': leaderboard_data,
        'members': leaderboard_data,
        'is_user_admin': membership.is_admin,
        'group_name': membership.group.name,
        'graph_data': chart_datasets
    })

@app.route('/api/groups/<int:group_id>/add_match', methods=['POST'])
@login_required
def add_match(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin:
        return jsonify({'error': 'Admin only'}), 403

    data = request.json
    try:
        match_date = datetime.strptime(data['date'], '%Y-%m-%dT%H:%M')
    except:
        match_date = datetime.utcnow()

    match = Match(
        group_id=group_id,
        winner_id=data['winner_id'],
        loser_id=data['loser_id'],
        score=data['score'],
        timestamp=match_date
    )
    db.session.add(match)
    db.session.commit()

    recalculate_group_elos(group_id)
    return jsonify({'message': 'Match added'})

@app.route('/api/matches/<int:match_id>', methods=['PUT', 'DELETE'])
@login_required
def manage_match(match_id):
    match = Match.query.get_or_404(match_id)
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=match.group_id).first()
    if not membership or not membership.is_admin:
        return jsonify({'error': 'Admin only'}), 403

    if request.method == 'DELETE':
        db.session.delete(match)
        db.session.commit()
        recalculate_group_elos(match.group_id)
        return jsonify({'message': 'Deleted'})

    if request.method == 'PUT':
        data = request.json
        match.winner_id = data['winner_id']
        match.loser_id = data['loser_id']
        match.score = data['score']
        try:
            match.timestamp = datetime.strptime(data['date'], '%Y-%m-%dT%H:%M')
        except:
            pass

        db.session.commit()
        recalculate_group_elos(match.group_id)
        return jsonify({'message': 'Updated'})

@app.route('/api/groups/<int:group_id>/add_member', methods=['POST'])
@login_required
def add_member(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin: return jsonify({'error': 'Admin only'}), 403

    user_to_add = User.query.filter_by(username=request.json['username']).first()
    if not user_to_add: return jsonify({'error': 'User not found'}), 404

    if GroupMember.query.filter_by(user_id=user_to_add.id, group_id=group_id).first():
        return jsonify({'error': 'Already in group'}), 400

    new_mem = GroupMember(user_id=user_to_add.id, group_id=group_id)
    db.session.add(new_mem)
    db.session.commit()
    return jsonify({'message': 'Member added'})

@app.route('/api/groups/<int:group_id>/make_admin', methods=['POST'])
@login_required
def make_admin(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin: return jsonify({'error': 'Admin only'}), 403

    target_mem = GroupMember.query.filter_by(user_id=request.json['user_id'], group_id=group_id).first()
    target_mem.is_admin = True
    db.session.commit()
    return jsonify({'message': 'Admin rights granted'})

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
