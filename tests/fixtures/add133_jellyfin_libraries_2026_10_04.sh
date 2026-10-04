set -e
export DEBIAN_FRONTEND=noninteractive
mkdir -p /var/lib/jellyfin/root/default/Movies /var/lib/jellyfin/root/default/Shows
printf '/media/movies\n' > /var/lib/jellyfin/root/default/Movies/movies.mblink
printf '/media/tv\n' > /var/lib/jellyfin/root/default/Shows/shows.mblink
[ -e "/var/lib/jellyfin/root/default/Movies/options.xml" ] && cp -a "/var/lib/jellyfin/root/default/Movies/options.xml" "/var/lib/jellyfin/root/default/Movies/options.xml.bak.$(date +%Y%m%d%H%M%S)"
cp /var/lib/jellyfin/root/default/Media/options.xml /var/lib/jellyfin/root/default/Movies/options.xml
[ -e "/var/lib/jellyfin/root/default/Shows/options.xml" ] && cp -a "/var/lib/jellyfin/root/default/Shows/options.xml" "/var/lib/jellyfin/root/default/Shows/options.xml.bak.$(date +%Y%m%d%H%M%S)"
cp /var/lib/jellyfin/root/default/Media/options.xml /var/lib/jellyfin/root/default/Shows/options.xml
[ -e "/var/lib/jellyfin/root/default/Movies/options.xml" ] && cp -a "/var/lib/jellyfin/root/default/Movies/options.xml" "/var/lib/jellyfin/root/default/Movies/options.xml.bak.$(date +%Y%m%d%H%M%S)"
sed -i 's|<ContentType>.*</ContentType>|<ContentType>movies</ContentType>|' /var/lib/jellyfin/root/default/Movies/options.xml
[ -e "/var/lib/jellyfin/root/default/Shows/options.xml" ] && cp -a "/var/lib/jellyfin/root/default/Shows/options.xml" "/var/lib/jellyfin/root/default/Shows/options.xml.bak.$(date +%Y%m%d%H%M%S)"
sed -i 's|<ContentType>.*</ContentType>|<ContentType>tvshows</ContentType>|' /var/lib/jellyfin/root/default/Shows/options.xml
chown -R jellyfin:jellyfin /var/lib/jellyfin/root/default/Movies /var/lib/jellyfin/root/default/Shows
systemctl restart jellyfin
