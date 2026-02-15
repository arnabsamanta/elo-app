import os
import math
import trueskill
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# --- CONFIG ---
app.config['SECRET_KEY'] = 'mysecretkey123'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_db.sqlite')
if app.config['SQLALCHEMY_DATABASE_URI'] and app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)

# TrueSkill Global Env
ts_env = trueskill.TrueSkill(draw_probability=0) # Assuming no draws for now, or low chance

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

    # TrueSkill Data
    mu = db.Column(db.Float, default=25.0)
    sigma = db.Column(db.Float, default=8.333)

    # Display Rating (mu - 3*sigma)
    rating = db.Column(db.Float, default=0.0)

    user = db.relationship('User', backref='memberships')
    group = db.relationship('Group', backref='members')

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=False)
    score = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    participants = db.relationship('MatchParticipant', backref='match', cascade="all, delete-orphan")

class MatchParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    match_id = db.Column(db.Integer, db.ForeignKey('match.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    is_winner = db.Column(db.Boolean, nullable=False) # True = Winner Team, False = Loser Team

    user = db.relationship('User')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- TRUESKILL LOGIC ---

def get_win_probability(team1, team2):
    """
    Calculates probability of Team 1 beating Team 2.
    team1/team2 are lists of GroupMember objects (or dicts with mu/sigma).
    """
    delta_mu = sum(p.mu for p in team1) - sum(p.mu for p in team2)
    sum_sigma = sum(p.sigma ** 2 for p in team1) + sum(p.sigma ** 2 for p in team2)
    player_count = len(team1) + len(team2)
    denominator = math.sqrt(player_count * (ts_env.beta ** 2) + sum_sigma)
    return ts_env.cdf(delta_mu / denominator)

def recalculate_group_ratings(group_id):
    # 1. Reset all members
    members = GroupMember.query.filter_by(group_id=group_id).all()
    # Map user_id to a Rating Object
    rating_map = {m.user_id: ts_env.create_rating() for m in members}

    # 2. Get matches sorted by time
    matches = Match.query.filter_by(group_id=group_id).all()
    matches.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)

    # 3. Replay History
    for m in matches:
        winners = [p.user_id for p in m.participants if p.is_winner]
        losers = [p.user_id for p in m.participants if not p.is_winner]

        if not winners or not losers: continue # Skip broken data

        # Construct Team Rating Lists
        winner_ratings = [rating_map[uid] for uid in winners if uid in rating_map]
        loser_ratings = [rating_map[uid] for uid in losers if uid in rating_map]

        if not winner_ratings or not loser_ratings: continue

        # Calculate new ratings
        # rate method expects list of teams: [team1_ratings, team2_ratings]
        # ranks=[0, 1] means team1 (index 0) beat team2 (index 1)
        new_ratings_list = ts_env.rate([winner_ratings, loser_ratings], ranks=[0, 1])

        # Update map
        new_winners = new_ratings_list[0]
        new_losers = new_ratings_list[1]

        for idx, uid in enumerate([uid for uid in winners if uid in rating_map]):
            rating_map[uid] = new_winners[idx]

        for idx, uid in enumerate([uid for uid in losers if uid in rating_map]):
            rating_map[uid] = new_losers[idx]

    # 4. Save to DB
    for m in members:
        r = rating_map.get(m.user_id)
        if r:
            m.mu = r.mu
            m.sigma = r.sigma
            # Conservative Rating: mu - 3*sigma
            m.rating = r.mu - 3 * r.sigma

    db.session.commit()

# --- ROUTES ---

@app.route('/')
def home(): return render_template('index.html')

@app.route('/api/session')
def check_session():
    if current_user.is_authenticated:
        return jsonify({'auth': True, 'username': current_user.username, 'id': current_user.id})
    return jsonify({'auth': False})

@app.route('/api/profile', methods=['PUT'])
@login_required
def update_profile():
    data = request.json
    new_username = data.get('username')
    new_password = data.get('password')
    if new_username and new_username != current_user.username:
        if User.query.filter_by(username=new_username).first():
            return jsonify({'error': 'Username taken'}), 400
        current_user.username = new_username
    if new_password:
        current_user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
    db.session.commit()
    return jsonify({'message': 'Profile updated', 'username': current_user.username})

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    try:
        if User.query.filter_by(username=data['username']).first():
            return jsonify({'error': 'Username taken'}), 400
        hashed_pw = generate_password_hash(data['password'], method='pbkdf2:sha256')
        new_user = User(username=data['username'], password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        return jsonify({'message': 'Created'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data['username']).first()
    if user and check_password_hash(user.password, data['password']):
        login_user(user, remember=True)
        return jsonify({'message': 'Logged in', 'username': user.username, 'id': user.id})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/logout')
@login_required
def logout(): logout_user(); return jsonify({'message': 'Logged out'})

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
    if not membership: return jsonify({'error': 'Unauthorized'}), 403

    # 1. Matches (N vs N)
    matches = Match.query.filter_by(group_id=group_id).all()
    matches.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min, reverse=True)

    matches_data = []
    for m in matches:
        winners = [p.user.username for p in m.participants if p.is_winner]
        losers = [p.user.username for p in m.participants if not p.is_winner]
        date_display = m.timestamp.strftime('%Y-%m-%d %H:%M') if m.timestamp else "No Date"

        matches_data.append({
            'id': m.id,
            'winners': ", ".join(winners),
            'losers': ", ".join(losers),
            'score': m.score,
            'date': date_display
        })

    # 2. Leaderboard
    members = GroupMember.query.filter_by(group_id=group_id).order_by(GroupMember.rating.desc()).all()
    leaderboard_data = []
    for m in members:
        leaderboard_data.append({
            'username': m.user.username,
            'rating': round(m.rating, 2), # Display conservative rating
            'is_admin': m.is_admin,
            'id': m.user_id,
            'mu': m.mu, # Need these for probability calc
            'sigma': m.sigma
        })

    # 3. Graph (Rating over time)
    # Replay history to get points
    matches.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)
    rating_map = {m.user_id: ts_env.create_rating() for m in members}
    daily_closing_ratings = {uid: {} for uid in rating_map}
    user_names = {m.user_id: m.user.username for m in members}

    for m in matches:
        w_ids = [p.user_id for p in m.participants if p.is_winner]
        l_ids = [p.user_id for p in m.participants if not p.is_winner]
        if not w_ids or not l_ids: continue

        w_team = [rating_map[uid] for uid in w_ids if uid in rating_map]
        l_team = [rating_map[uid] for uid in l_ids if uid in rating_map]

        if w_team and l_team:
            new_ratings = ts_env.rate([w_team, l_team], ranks=[0, 1])

            # Update Map & Record Data Point
            ts = m.timestamp if m.timestamp else datetime.utcnow()
            day_key = ts.strftime('%Y-%m-%d')

            # Update Winners
            for idx, uid in enumerate([uid for uid in w_ids if uid in rating_map]):
                rating_map[uid] = new_ratings[0][idx]
                r = rating_map[uid]
                daily_closing_ratings[uid][day_key] = r.mu - 3*r.sigma

            # Update Losers
            for idx, uid in enumerate([uid for uid in l_ids if uid in rating_map]):
                rating_map[uid] = new_ratings[1][idx]
                r = rating_map[uid]
                daily_closing_ratings[uid][day_key] = r.mu - 3*r.sigma

    chart_datasets = []
    colors = ['#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF', '#FF9F40']
    for idx, (uid, date_map) in enumerate(daily_closing_ratings.items()):
        data_points = []
        for d in sorted(date_map.keys()):
            data_points.append({'x': d, 'y': date_map[d]})
        if data_points:
            chart_datasets.append({
                'label': user_names.get(uid, 'Unknown'),
                'data': data_points,
                'borderColor': colors[idx % len(colors)],
                'fill': False,
                'tension': 0.1,
                'pointRadius': 3
            })

    return jsonify({
        'matches': matches_data,
        'leaderboard': leaderboard_data,
        'members': leaderboard_data, # Use same list for member dropdowns
        'is_user_admin': membership.is_admin,
        'group_name': membership.group.name,
        'graph_data': chart_datasets
    })

@app.route('/api/predict', methods=['POST'])
@login_required
def predict_match():
    data = request.json
    # Expect data = { 'team1_ids': [1, 2], 'team2_ids': [3, 4], 'group_id': 1 }
    group_id = data.get('group_id')

    # Fetch Members
    t1_members = GroupMember.query.filter(GroupMember.group_id==group_id, GroupMember.user_id.in_(data['team1_ids'])).all()
    t2_members = GroupMember.query.filter(GroupMember.group_id==group_id, GroupMember.user_id.in_(data['team2_ids'])).all()

    if not t1_members or not t2_members:
        return jsonify({'win_prob': 0.5}) # Default

    prob = get_win_probability(t1_members, t2_members)
    return jsonify({'win_prob': prob})

@app.route('/api/groups/<int:group_id>/add_match', methods=['POST'])
@login_required
def add_match(group_id):
    # Data: { winners: [id, id], losers: [id, id], score: "...", date: "..." }
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin: return jsonify({'error': 'Admin only'}), 403

    data = request.json
    try: match_date = datetime.strptime(data['date'], '%Y-%m-%dT%H:%M')
    except: match_date = datetime.utcnow()

    # Create Match
    match = Match(group_id=group_id, score=data['score'], timestamp=match_date)
    db.session.add(match)
    db.session.flush() # Get ID

    # Add Participants
    for uid in data['winners']:
        db.session.add(MatchParticipant(match_id=match.id, user_id=uid, is_winner=True))
    for uid in data['losers']:
        db.session.add(MatchParticipant(match_id=match.id, user_id=uid, is_winner=False))

    db.session.commit()
    recalculate_group_ratings(group_id)
    return jsonify({'message': 'Match added'})

@app.route('/api/matches/<int:match_id>', methods=['DELETE'])
@login_required
def delete_match(match_id):
    match = Match.query.get_or_404(match_id)
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=match.group_id).first()
    if not membership or not membership.is_admin: return jsonify({'error': 'Admin only'}), 403

    db.session.delete(match)
    db.session.commit()
    recalculate_group_ratings(match.group_id)
    return jsonify({'message': 'Deleted'})

# Edit is complex for N vs N, keeping it simple for now (Delete & Re-add is safer)

@app.route('/api/groups/<int:group_id>/add_member', methods=['POST'])
@login_required
def add_member(group_id):
    membership = GroupMember.query.filter_by(user_id=current_user.id, group_id=group_id).first()
    if not membership or not membership.is_admin: return jsonify({'error': 'Admin only'}), 403
    user_to_add = User.query.filter_by(username=request.json['username']).first()
    if not user_to_add: return jsonify({'error': 'User not found'}), 404
    if GroupMember.query.filter_by(user_id=user_to_add.id, group_id=group_id).first():
        return jsonify({'error': 'Already in group'}), 400
    # New members get default TrueSkill
    db.session.add(GroupMember(user_id=user_to_add.id, group_id=group_id))
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
